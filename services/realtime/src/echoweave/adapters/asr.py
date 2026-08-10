from __future__ import annotations

import asyncio
import contextlib
import io
import json
import wave

from echoweave.adapters.http import (
    ManagedAsyncClient,
    read_bounded_body,
    require_content_type,
)
from echoweave.contracts import Transcript


def pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)
    return output.getvalue()


class DemoASR:
    async def transcribe(self, pcm16: bytes, sample_rate: int) -> Transcript:
        duration = len(pcm16) / max(1, sample_rate * 2)
        return Transcript(
            text=f"你好，这是 {duration:.1f} 秒的实时语音测试。",
            language="Chinese",
        )


class Qwen3ASRHTTP:
    """OpenAI transcription API exposed by vLLM / qwen-asr-serve."""

    def __init__(
        self,
        base_url: str,
        model: str = "Qwen/Qwen3-ASR-1.7B",
        api_key: str = "EMPTY",
        timeout_seconds: float = 120.0,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._http = ManagedAsyncClient(timeout_seconds)

    async def transcribe(self, pcm16: bytes, sample_rate: int) -> Transcript:
        wav = pcm16_to_wav(pcm16, sample_rate)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        client = await self._http.get()
        async with client.stream(
            "POST",
            f"{self.base_url}/audio/transcriptions",
            headers=headers,
            data={"model": self.model},
            files={"file": ("speech.wav", wav, "audio/wav")},
        ) as response:
            response.raise_for_status()
            require_content_type(
                response,
                {"application/json"},
                source="ASR worker",
            )
            raw_payload = await read_bounded_body(
                response,
                self.max_response_bytes,
                source="ASR worker",
            )
        try:
            payload = json.loads(raw_payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("ASR worker returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("ASR worker returned an invalid response")
        raw_text = payload.get("text", "")
        if not isinstance(raw_text, str):
            raise TypeError("ASR worker returned a non-text transcript")
        raw_language = payload.get("language")
        return Transcript(
            text=raw_text.strip(),
            language=raw_language if isinstance(raw_language, str) else None,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class Qwen3ASRLocal:
    """Official qwen-asr package integration; model loads on first request."""

    _model = None
    _load_lock = asyncio.Lock()
    _inference_lock = asyncio.Lock()

    def __init__(self, model_name: str = "Qwen/Qwen3-ASR-1.7B") -> None:
        self.model_name = model_name

    async def _get_model(self):
        if self.__class__._model is not None:
            return self.__class__._model
        async with self.__class__._load_lock:
            if self.__class__._model is None:
                self.__class__._model = await asyncio.to_thread(self._load_model)
        return self.__class__._model

    def _load_model(self):
        try:
            import torch
            from qwen_asr import Qwen3ASRModel
        except ImportError as exc:
            raise RuntimeError("Qwen ASR is not installed. Install `.[qwen]`.") from exc
        return Qwen3ASRModel.from_pretrained(
            self.model_name,
            dtype=torch.bfloat16,
            device_map="cuda:0",
            max_inference_batch_size=1,
            max_new_tokens=256,
        )

    async def transcribe(self, pcm16: bytes, sample_rate: int) -> Transcript:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Qwen ASR requires numpy.") from exc
        model = await self._get_model()
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        async with self.__class__._inference_lock:
            inference = asyncio.create_task(
                asyncio.to_thread(
                    model.transcribe,
                    audio=(audio, sample_rate),
                    language=None,
                )
            )
            try:
                results = await asyncio.shield(inference)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await inference
                raise
        result = results[0]
        return Transcript(text=result.text.strip(), language=result.language)
