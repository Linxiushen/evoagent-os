from __future__ import annotations

import asyncio
import base64
import contextlib
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx

from echoweave.adapters.http import (
    ManagedAsyncClient,
    iter_bounded_raw,
    require_content_type,
)
from echoweave.contracts import AudioFrame, PersonaProfile


def _reference_voice_bytes(
    persona: PersonaProfile,
) -> tuple[bytes | None, str | None]:
    captured = getattr(persona, "reference_voice_data", None)
    captured_name = getattr(persona, "reference_voice_name", None)
    if captured is not None:
        return captured, captured_name or "reference.wav"
    if persona.reference_voice is None:
        return None, None
    return persona.reference_voice.read_bytes(), persona.reference_voice.name


def _write_private_reference(data: bytes, suffix: str) -> Path:
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="echoweave-voice-",
            suffix=suffix,
            delete=False,
        ) as handle:
            path = Path(handle.name)
            handle.write(data)
        path.chmod(0o600)
        return path
    except OSError:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


class BrowserTTS:
    browser_fallback = True

    async def synthesize(
        self,
        text: str,
        persona: PersonaProfile,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[AudioFrame]:
        if False:
            yield AudioFrame(b"", 24_000)


class VoxCPM2HTTP:
    """OpenAI-compatible `/v1/audio/speech` adapter for vLLM-Omni."""

    browser_fallback = False

    def __init__(
        self,
        base_url: str,
        model: str = "openbmb/VoxCPM2",
        api_key: str = "EMPTY",
        sample_rate: int = 48_000,
        worker_token: str = "",
        max_output_seconds: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.worker_token = worker_token
        self.max_output_bytes = sample_rate * 2 * max_output_seconds
        self._http = ManagedAsyncClient(httpx.Timeout(300.0, connect=15.0))

    async def synthesize(
        self,
        text: str,
        persona: PersonaProfile,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[AudioFrame]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.worker_token:
            headers["X-Worker-Token"] = self.worker_token
        body = {
            "model": self.model,
            "input": text,
            "voice": "default",
            "response_format": "pcm",
            "stream": True,
            "stream_format": "audio",
        }
        reference_voice, reference_name = _reference_voice_bytes(persona)
        if reference_voice is not None:
            suffix = Path(reference_name or "reference.wav").suffix.lower()
            mime_type = {
                ".flac": "audio/flac",
                ".mp3": "audio/mpeg",
                ".ogg": "audio/ogg",
                ".wav": "audio/wav",
            }.get(suffix, "audio/wav")
            encoded = base64.b64encode(reference_voice).decode("ascii")
            body["ref_audio"] = f"data:{mime_type};base64,{encoded}"
        if persona.reference_voice_transcript:
            body["ref_text"] = persona.reference_voice_transcript
        client = await self._http.get()
        async with client.stream(
            "POST",
            f"{self.base_url}/audio/speech",
            headers=headers,
            json=body,
        ) as response:
            response.raise_for_status()
            require_content_type(
                response,
                {"application/octet-stream", "audio/l16", "audio/pcm"},
                source="TTS worker",
            )
            announced_rate = response.headers.get("x-audio-sample-rate")
            if announced_rate:
                try:
                    parsed_rate = int(announced_rate)
                except ValueError as exc:
                    raise RuntimeError(
                        "TTS worker returned an invalid sample rate"
                    ) from exc
                if parsed_rate != self.sample_rate:
                    raise RuntimeError(
                        "TTS worker sample rate does not match configuration"
                    )
            emitted_bytes = 0
            pending = b""
            async for chunk in iter_bounded_raw(
                response,
                self.max_output_bytes,
                source="TTS worker",
            ):
                if cancel_event.is_set():
                    return
                offset = 0
                if pending:
                    pcm = pending + chunk[:1]
                    pending = b""
                    offset = 1
                    pts_ms = int(emitted_bytes * 1000 / (self.sample_rate * 2))
                    yield AudioFrame(pcm, self.sample_rate, pts_ms=pts_ms)
                    emitted_bytes += len(pcm)
                while offset + 1 < len(chunk):
                    if cancel_event.is_set():
                        return
                    end = min(offset + 8192, len(chunk))
                    end -= (end - offset) % 2
                    pcm = bytes(memoryview(chunk)[offset:end])
                    pts_ms = int(emitted_bytes * 1000 / (self.sample_rate * 2))
                    yield AudioFrame(pcm, self.sample_rate, pts_ms=pts_ms)
                    emitted_bytes += len(pcm)
                    offset = end
                if offset < len(chunk):
                    pending = chunk[offset:]
            if pending:
                raise RuntimeError("TTS worker returned a truncated PCM16 sample")

    async def aclose(self) -> None:
        await self._http.aclose()


class VoxCPM2Local:
    """Official `voxcpm` streaming API with consent-bound reference audio."""

    browser_fallback = False
    _model = None
    _load_lock = asyncio.Lock()
    _inference_lock = asyncio.Lock()

    def __init__(self, model_name: str = "openbmb/VoxCPM2") -> None:
        self.model_name = model_name

    def _load_model(self):
        try:
            from voxcpm import VoxCPM
        except ImportError as exc:
            raise RuntimeError(
                "VoxCPM2 is not installed. Install `.[voxcpm]`."
            ) from exc
        return VoxCPM.from_pretrained(self.model_name, load_denoiser=False)

    async def _get_model(self):
        if self.__class__._model is not None:
            return self.__class__._model
        async with self.__class__._load_lock:
            if self.__class__._model is None:
                self.__class__._model = await asyncio.to_thread(self._load_model)
        return self.__class__._model

    @staticmethod
    def _next_chunk(iterator: Iterator):
        try:
            return False, next(iterator)
        except StopIteration:
            return True, None

    async def synthesize(
        self,
        text: str,
        persona: PersonaProfile,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[AudioFrame]:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("VoxCPM2 requires numpy.") from exc
        model = await self._get_model()
        captured_voice, captured_name = _reference_voice_bytes(persona)
        temporary_reference: Path | None = None
        reference_path = persona.reference_voice
        if captured_voice is not None:
            suffix = Path(captured_name or "reference.wav").suffix.lower()
            if suffix not in {".flac", ".mp3", ".ogg", ".wav"}:
                suffix = ".wav"
            temporary_reference = _write_private_reference(captured_voice, suffix)
            reference_path = temporary_reference
        kwargs = {"text": text}
        if reference_path:
            kwargs["reference_wav_path"] = str(reference_path)
        if reference_path and persona.reference_voice_transcript:
            kwargs.update(
                {
                    "prompt_wav_path": str(reference_path),
                    "prompt_text": persona.reference_voice_transcript,
                }
            )
        try:
            async with self.__class__._inference_lock:
                iterator = iter(model.generate_streaming(**kwargs))
                sample_rate = int(model.tts_model.sample_rate)
                pts_ms = 0
                try:
                    while not cancel_event.is_set():
                        next_chunk = asyncio.create_task(
                            asyncio.to_thread(self._next_chunk, iterator)
                        )
                        try:
                            done, chunk = await asyncio.shield(next_chunk)
                        except asyncio.CancelledError:
                            with contextlib.suppress(Exception):
                                await next_chunk
                            raise
                        if done:
                            break
                        values = np.clip(np.asarray(chunk), -1.0, 1.0)
                        pcm = (values * 32767.0).astype("<i2").tobytes()
                        yield AudioFrame(pcm, sample_rate, pts_ms=pts_ms)
                        pts_ms += int(len(pcm) * 1000 / (sample_rate * 2))
                finally:
                    close = getattr(iterator, "close", None)
                    if close is not None:
                        await asyncio.to_thread(close)
        finally:
            if temporary_reference is not None:
                temporary_reference.unlink(missing_ok=True)
