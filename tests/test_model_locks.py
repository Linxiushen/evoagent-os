import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from echoweave.adapters.asr import Qwen3ASRLocal
from echoweave.adapters.avatar import _reference_image_bytes
from echoweave.adapters.tts import VoxCPM2Local
from echoweave.contracts import PersonaProfile


class ConcurrencyProbe:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def enter(self):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    def exit(self):
        with self.lock:
            self.active -= 1


async def test_qwen_local_cancellation_keeps_shared_model_serialized():
    probe = ConcurrencyProbe()

    class FakeModel:
        def transcribe(self, **_kwargs):
            probe.enter()
            try:
                time.sleep(0.12)
                return [SimpleNamespace(text="ok", language="Chinese")]
            finally:
                probe.exit()

    previous = Qwen3ASRLocal._model
    Qwen3ASRLocal._model = FakeModel()
    try:
        first_adapter = Qwen3ASRLocal()
        second_adapter = Qwen3ASRLocal()
        first = asyncio.create_task(first_adapter.transcribe(b"\x00\x00" * 512, 16_000))
        await asyncio.sleep(0.02)
        first.cancel()
        second = asyncio.create_task(
            second_adapter.transcribe(b"\x00\x00" * 512, 16_000)
        )
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1].text == "ok"
        assert probe.maximum == 1
    finally:
        Qwen3ASRLocal._model = previous


async def test_voxcpm_local_cancellation_keeps_shared_model_serialized():
    probe = ConcurrencyProbe()

    class FakeIterator:
        def __init__(self):
            self.remaining = 2

        def __iter__(self):
            return self

        def __next__(self):
            if self.remaining == 0:
                raise StopIteration
            self.remaining -= 1
            probe.enter()
            try:
                time.sleep(0.12)
                return [0.0] * 480
            finally:
                probe.exit()

        def close(self):
            self.remaining = 0

    class FakeModel:
        tts_model = SimpleNamespace(sample_rate=48_000)

        def generate_streaming(self, **_kwargs):
            return FakeIterator()

    async def consume_one(adapter):
        async for _frame in adapter.synthesize(
            "test",
            PersonaProfile(
                persona_id="demo",
                display_name="Echo",
                system_prompt="system",
                disclosure_text="AI",
                is_fictional=True,
            ),
            asyncio.Event(),
        ):
            return

    previous = VoxCPM2Local._model
    VoxCPM2Local._model = FakeModel()
    try:
        first_adapter = VoxCPM2Local()
        second_adapter = VoxCPM2Local()
        first = asyncio.create_task(consume_one(first_adapter))
        await asyncio.sleep(0.02)
        first.cancel()
        second = asyncio.create_task(consume_one(second_adapter))
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert probe.maximum == 1
    finally:
        VoxCPM2Local._model = previous


async def test_voxcpm_local_prefers_captured_voice_and_cleans_private_copy(tmp_path):
    original_path = tmp_path / "voice.wav"
    original_path.write_bytes(b"mutated source")
    captured_voice = b"RIFF\x04\x00\x00\x00WAVEauthorized snapshot"
    observed: dict[str, object] = {}

    class FakeIterator:
        def __init__(self):
            self.finished = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.finished:
                raise StopIteration
            self.finished = True
            return [0.0] * 48

        def close(self):
            self.finished = True

    class FakeModel:
        tts_model = SimpleNamespace(sample_rate=48_000)

        def generate_streaming(self, **kwargs):
            private_path = Path(kwargs["reference_wav_path"])
            observed["path"] = private_path
            observed["data"] = private_path.read_bytes()
            return FakeIterator()

    persona = PersonaProfile(
        persona_id="authorized",
        display_name="Authorized",
        system_prompt="system",
        disclosure_text="AI",
        is_fictional=False,
        reference_voice=original_path,
        reference_voice_data=captured_voice,
        reference_voice_name="voice.wav",
    )
    previous = VoxCPM2Local._model
    VoxCPM2Local._model = FakeModel()
    try:
        frames = [
            frame
            async for frame in VoxCPM2Local().synthesize(
                "test",
                persona,
                asyncio.Event(),
            )
        ]
    finally:
        VoxCPM2Local._model = previous

    assert frames
    assert observed["data"] == captured_voice
    assert isinstance(observed["path"], Path)
    assert not observed["path"].exists()
    assert original_path.read_bytes() == b"mutated source"


def test_avatar_prefers_captured_image_bytes(tmp_path):
    original_path = tmp_path / "face.png"
    original_path.write_bytes(b"mutated source")
    persona = PersonaProfile(
        persona_id="authorized",
        display_name="Authorized",
        system_prompt="system",
        disclosure_text="AI",
        is_fictional=False,
        reference_image=original_path,
        reference_image_data=b"\x89PNG\r\n\x1a\nauthorized snapshot",
        reference_image_name="face.png",
    )

    data, name = _reference_image_bytes(persona)

    assert data == b"\x89PNG\r\n\x1a\nauthorized snapshot"
    assert name == "face.png"
