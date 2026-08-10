from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator

import httpx

from echoweave.adapters.asr import pcm16_to_wav
from echoweave.adapters.http import (
    ManagedAsyncClient,
    iter_bounded_lines,
    require_content_type,
)
from echoweave.contracts import AvatarSegment, PersonaProfile


def _reference_image_bytes(
    persona: PersonaProfile,
) -> tuple[bytes | None, str | None]:
    captured = getattr(persona, "reference_image_data", None)
    captured_name = getattr(persona, "reference_image_name", None)
    if captured is not None:
        return captured, captured_name or "avatar.png"
    if persona.reference_image is None:
        return None, None
    return persona.reference_image.read_bytes(), persona.reference_image.name


class ClientLipSyncAvatar:
    """Safe demo fallback: browser animates a clearly synthetic face."""

    synchronized_playback = False

    async def animate(
        self,
        text: str,
        audio_pcm16: bytes,
        sample_rate: int,
        persona: PersonaProfile,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[AvatarSegment]:
        duration_ms = (
            int(len(audio_pcm16) * 1000 / max(1, sample_rate * 2))
            if audio_pcm16
            else max(650, len(text) * 110)
        )
        yield AvatarSegment(
            kind="client_lipsync",
            duration_ms=duration_ms,
            metadata={"synthetic": True, "watermark": "AI 数字分身"},
        )


class SoulXHTTPAvatar:
    """Streams NDJSON video segments from `services/soulx_api.py`."""

    synchronized_playback = True

    def __init__(
        self,
        base_url: str,
        worker_token: str = "",
        max_segment_bytes: int = 32 * 1024 * 1024,
        max_response_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_token = worker_token
        self.max_segment_bytes = max_segment_bytes
        self.max_response_bytes = max_response_bytes
        self.max_line_bytes = 4 * ((max_segment_bytes + 2) // 3) + 16 * 1024
        self._http = ManagedAsyncClient(httpx.Timeout(600.0, connect=15.0))

    async def animate(
        self,
        text: str,
        audio_pcm16: bytes,
        sample_rate: int,
        persona: PersonaProfile,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[AvatarSegment]:
        reference_image, reference_name = _reference_image_bytes(persona)
        if reference_image is None:
            raise RuntimeError("SoulX requires a consent-bound reference image")
        if not audio_pcm16:
            return
        suffix = (reference_name or "avatar.png").lower().rsplit(".", 1)[-1]
        image_mime_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }.get(suffix, "image/png")
        files = {
            "image": (
                reference_name or "avatar.png",
                reference_image,
                image_mime_type,
            ),
            "audio": (
                "speech.wav",
                pcm16_to_wav(audio_pcm16, sample_rate),
                "audio/wav",
            ),
        }
        data = {"model_type": "lite", "text": text}
        headers = {}
        if self.worker_token:
            headers["X-Worker-Token"] = self.worker_token
        client = await self._http.get()
        async with client.stream(
            "POST",
            f"{self.base_url}/v1/avatar/stream",
            headers=headers,
            data=data,
            files=files,
        ) as response:
            response.raise_for_status()
            require_content_type(
                response,
                {"application/ndjson", "application/x-ndjson"},
                source="SoulX worker",
            )
            expected_index = 0
            async for raw_line in iter_bounded_lines(
                response,
                max_line_bytes=self.max_line_bytes,
                max_total_bytes=self.max_response_bytes,
                source="SoulX worker",
            ):
                if cancel_event.is_set():
                    return
                if not raw_line:
                    continue
                try:
                    line = raw_line.decode("utf-8", errors="strict")
                    payload = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("SoulX worker returned invalid NDJSON") from exc
                if not isinstance(payload, dict):
                    raise TypeError("SoulX worker returned an invalid event")
                segment_data = payload.get("data_b64")
                if segment_data is not None and not isinstance(segment_data, str):
                    raise RuntimeError("SoulX worker returned invalid segment data")
                try:
                    decoded = (
                        base64.b64decode(segment_data, validate=True)
                        if segment_data
                        else None
                    )
                except (binascii.Error, ValueError) as exc:
                    raise RuntimeError(
                        "SoulX worker returned invalid segment data"
                    ) from exc
                if decoded and len(decoded) > self.max_segment_bytes:
                    raise RuntimeError("SoulX worker returned an oversized segment")
                mime_type = payload.get("mime_type", "video/mp4")
                if mime_type != "video/mp4":
                    raise RuntimeError(
                        "SoulX worker returned an unsupported media type"
                    )
                try:
                    index = int(payload.get("index", -1))
                    duration_ms = int(payload.get("duration_ms", 0))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "SoulX worker returned invalid metadata"
                    ) from exc
                if index != expected_index:
                    raise RuntimeError("SoulX worker returned an out-of-order segment")
                if duration_ms < 0 or duration_ms > 60_000:
                    raise RuntimeError("SoulX worker returned an invalid duration")
                expected_index += 1
                yield AvatarSegment(
                    kind="soulx_mp4",
                    index=index,
                    data=decoded,
                    url=None,
                    mime_type=mime_type,
                    duration_ms=duration_ms,
                    metadata={"synthetic": True, "watermark": "AI 数字分身"},
                )

    async def aclose(self) -> None:
        await self._http.aclose()
