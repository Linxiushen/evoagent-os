from __future__ import annotations

import hashlib
import importlib.util
import json
import wave
from pathlib import Path

import pytest

from echoweave.contracts import VADDecision

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "segment_silero_ranges.py"
SPEC = importlib.util.spec_from_file_location("segment_silero_ranges", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _write_wav(path: Path, *, frame_count: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(MODULE.SAMPLE_RATE)
        stream.writeframes(b"\x10\x00" * frame_count)


class FakeVAD:
    min_silence_samples = 512
    speech_pad_samples = 0

    def __init__(self, events: dict[int, tuple[bool, bool]]) -> None:
        self.events = events
        self.calls = 0

    async def process(self, pcm16: bytes, sample_rate: int) -> VADDecision:
        assert len(pcm16) == MODULE.FRAME_SAMPLES * 2
        assert sample_rate == MODULE.SAMPLE_RATE
        self.calls += 1
        started, ended = self.events.get(self.calls, (False, False))
        return VADDecision(0.9 if started or not ended else 0.1, started, ended)


@pytest.mark.asyncio
async def test_segment_audio_writes_bound_ranges(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    output = tmp_path / "ranges.json"
    frame_count = MODULE.FRAME_SAMPLES * 400
    _write_wav(audio, frame_count=frame_count)
    vad = FakeVAD({1: (True, False), 200: (False, True), 220: (True, False)})

    await MODULE.segment_audio(
        audio_path=audio,
        output_path=output,
        clip_prefix="sample",
        min_clip_seconds=3,
        max_clip_seconds=30,
        merge_gap_ms=0,
        vad=vad,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["source_sha256"] == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert payload["segments"] == [
        {
            "clip_id": "sample-0001",
            "start_seconds": 0.0,
            "end_seconds": 6.368,
        },
        {
            "clip_id": "sample-0002",
            "start_seconds": 7.008,
            "end_seconds": 12.8,
        },
    ]


def test_split_keeps_every_clip_within_contract() -> None:
    spans = MODULE._split_and_filter_spans(
        [(0, MODULE.SAMPLE_RATE * 62)],
        min_samples=MODULE.SAMPLE_RATE * 3,
        max_samples=MODULE.SAMPLE_RATE * 30,
    )
    assert spans == [
        (0, MODULE.SAMPLE_RATE * 30),
        (MODULE.SAMPLE_RATE * 30, MODULE.SAMPLE_RATE * 59),
        (MODULE.SAMPLE_RATE * 59, MODULE.SAMPLE_RATE * 62),
    ]


@pytest.mark.asyncio
async def test_rejects_existing_output(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    output = tmp_path / "ranges.json"
    _write_wav(audio, frame_count=MODULE.SAMPLE_RATE * 4)
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(MODULE.SegmentationError, match="already exists"):
        await MODULE.segment_audio(
            audio_path=audio,
            output_path=output,
            clip_prefix="sample",
            vad=FakeVAD({1: (True, False)}),
        )
    assert output.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_rejects_non_pcm16_wav(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    output = tmp_path / "ranges.json"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(MODULE.SAMPLE_RATE)
        stream.writeframes(b"\x00\x00\x00\x00" * MODULE.SAMPLE_RATE * 3)

    with pytest.raises(MODULE.SegmentationError, match="mono PCM16"):
        await MODULE.segment_audio(
            audio_path=audio,
            output_path=output,
            clip_prefix="sample",
            vad=FakeVAD({1: (True, False)}),
        )
