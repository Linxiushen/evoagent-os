from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class SessionState(str, Enum):
    READY = "ready"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CLOSED = "closed"


@dataclass(slots=True)
class VADDecision:
    probability: float
    speech_started: bool = False
    speech_ended: bool = False


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None = None


@dataclass(slots=True)
class AudioFrame:
    pcm: bytes
    sample_rate: int
    channels: int = 1
    codec: str = "pcm_s16le"
    pts_ms: int = 0


@dataclass(slots=True)
class AvatarSegment:
    kind: str
    index: int = 0
    url: str | None = None
    data: bytes | None = None
    mime_type: str = "video/mp4"
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PersonaProfile:
    persona_id: str
    display_name: str
    system_prompt: str
    disclosure_text: str
    is_fictional: bool
    reference_image: Path | None = None
    reference_voice: Path | None = None
    reference_image_data: bytes | None = field(default=None, repr=False)
    reference_image_name: str | None = None
    reference_voice_data: bytes | None = field(default=None, repr=False)
    reference_voice_name: str | None = None
    reference_voice_transcript: str | None = None
    consent_id: str | None = None


class VADAdapter(Protocol):
    async def process(self, pcm16: bytes, sample_rate: int) -> VADDecision: ...

    def reset(self) -> None: ...


class ASRAdapter(Protocol):
    async def transcribe(self, pcm16: bytes, sample_rate: int) -> Transcript: ...


class LLMAdapter(Protocol):
    def stream(
        self, messages: list[dict[str, str]], cancel_event: object
    ) -> AsyncIterator[str]: ...


class TTSAdapter(Protocol):
    browser_fallback: bool

    def synthesize(
        self,
        text: str,
        persona: PersonaProfile,
        cancel_event: object,
    ) -> AsyncIterator[AudioFrame]: ...


class AvatarAdapter(Protocol):
    def animate(
        self,
        text: str,
        audio_pcm16: bytes,
        sample_rate: int,
        persona: PersonaProfile,
        cancel_event: object,
    ) -> AsyncIterator[AvatarSegment]: ...
