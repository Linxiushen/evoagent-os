from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_CHOICES = {
    "vad": {"energy", "silero_v5"},
    "asr": {"demo", "qwen_local", "qwen_http"},
    "llm": {"demo", "deepseek"},
    "tts": {"browser", "voxcpm_local", "voxcpm_http"},
    "avatar": {"client_lipsync", "soulx_http"},
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _tuple_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    )


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    persona_root: Path = Path("personas")
    consent_signing_key: str = ""
    consent_state_path: Path | None = None
    access_token: str = ""
    allowed_origins: tuple[str, ...] = ()
    allowed_personas: tuple[str, ...] = ("demo",)
    max_connections_per_ip: int = 4
    max_ws_message_bytes: int = 262_144
    max_text_chars: int = 1_000
    max_utterance_seconds: int = 60
    max_session_seconds: int = 1_800

    vad_backend: str = "energy"
    asr_backend: str = "demo"
    llm_backend: str = "demo"
    tts_backend: str = "browser"
    avatar_backend: str = "client_lipsync"

    qwen_model: str = "Qwen/Qwen3-ASR-1.7B"
    qwen_base_url: str = "http://127.0.0.1:8001/v1"
    qwen_api_key: str = "EMPTY"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking: str = "disabled"

    voxcpm_model: str = "openbmb/VoxCPM2"
    voxcpm_base_url: str = "http://127.0.0.1:8002/v1"
    voxcpm_api_key: str = "EMPTY"
    voxcpm_sample_rate: int = 48_000
    model_worker_token: str = ""

    soulx_base_url: str = "http://127.0.0.1:8003"

    @classmethod
    def from_env(cls) -> Settings:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        return cls(
            host=_env("ECHOWEAVE_HOST", "127.0.0.1"),
            port=int(_env("ECHOWEAVE_PORT", "8765")),
            log_level=_env("ECHOWEAVE_LOG_LEVEL", "INFO").upper(),
            persona_root=Path(_env("ECHOWEAVE_PERSONA_ROOT", "personas")).resolve(),
            consent_signing_key=_env("ECHOWEAVE_CONSENT_SIGNING_KEY"),
            consent_state_path=Path(
                _env(
                    "ECHOWEAVE_CONSENT_STATE_PATH",
                    "runtime/consent-state.json",
                )
            ).resolve(),
            access_token=_env("ECHOWEAVE_ACCESS_TOKEN"),
            allowed_origins=_tuple_env(
                "ECHOWEAVE_ALLOWED_ORIGINS",
                "",
            ),
            allowed_personas=_tuple_env("ECHOWEAVE_ALLOWED_PERSONAS", "demo"),
            max_connections_per_ip=int(_env("ECHOWEAVE_MAX_CONNECTIONS_PER_IP", "4")),
            max_ws_message_bytes=int(_env("ECHOWEAVE_MAX_WS_MESSAGE_BYTES", "262144")),
            max_text_chars=int(_env("ECHOWEAVE_MAX_TEXT_CHARS", "1000")),
            max_utterance_seconds=int(_env("ECHOWEAVE_MAX_UTTERANCE_SECONDS", "60")),
            max_session_seconds=int(_env("ECHOWEAVE_MAX_SESSION_SECONDS", "1800")),
            vad_backend=_env("ECHOWEAVE_VAD_BACKEND", "energy"),
            asr_backend=_env("ECHOWEAVE_ASR_BACKEND", "demo"),
            llm_backend=_env("ECHOWEAVE_LLM_BACKEND", "demo"),
            tts_backend=_env("ECHOWEAVE_TTS_BACKEND", "browser"),
            avatar_backend=_env("ECHOWEAVE_AVATAR_BACKEND", "client_lipsync"),
            qwen_model=_env("QWEN_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B"),
            qwen_base_url=_env("QWEN_ASR_BASE_URL", "http://127.0.0.1:8001/v1").rstrip(
                "/"
            ),
            qwen_api_key=_env("QWEN_ASR_API_KEY", "EMPTY"),
            deepseek_api_key=_env("DEEPSEEK_API_KEY"),
            deepseek_base_url=_env(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_thinking=_env("DEEPSEEK_THINKING", "disabled"),
            voxcpm_model=_env("VOXCPM_MODEL", "openbmb/VoxCPM2"),
            voxcpm_base_url=_env("VOXCPM_BASE_URL", "http://127.0.0.1:8002/v1").rstrip(
                "/"
            ),
            voxcpm_api_key=_env("VOXCPM_API_KEY", "EMPTY"),
            voxcpm_sample_rate=int(_env("VOXCPM_SAMPLE_RATE", "48000")),
            model_worker_token=_env("MODEL_WORKER_TOKEN"),
            soulx_base_url=_env("SOULX_BASE_URL", "http://127.0.0.1:8003").rstrip("/"),
        )

    def validate_bind_host(self, host: str) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"} and not self.access_token:
            raise RuntimeError(
                "ECHOWEAVE_ACCESS_TOKEN is required before binding beyond localhost"
            )

    def validate(self) -> None:
        for label, value in (
            ("vad", self.vad_backend),
            ("asr", self.asr_backend),
            ("llm", self.llm_backend),
            ("tts", self.tts_backend),
            ("avatar", self.avatar_backend),
        ):
            if value not in BACKEND_CHOICES[label]:
                raise ValueError(f"unsupported {label} backend: {value}")
        if not 1 <= self.port <= 65_535:
            raise ValueError("ECHOWEAVE_PORT must be between 1 and 65535")
        if self.access_token and len(self.access_token.encode("utf-8")) < 32:
            raise ValueError("ECHOWEAVE_ACCESS_TOKEN must be at least 32 bytes")
        if (
            self.model_worker_token
            and len(self.model_worker_token.encode("utf-8")) < 32
        ):
            raise ValueError("MODEL_WORKER_TOKEN must be at least 32 bytes")
        if (
            self.consent_signing_key
            and len(self.consent_signing_key.encode("utf-8")) < 32
        ):
            raise ValueError("ECHOWEAVE_CONSENT_SIGNING_KEY must be at least 32 bytes")
        if self.consent_signing_key and self.consent_state_path is None:
            raise ValueError(
                "ECHOWEAVE_CONSENT_STATE_PATH is required with a consent signing key"
            )
        if self.llm_backend == "deepseek" and not self.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek backend")
        if self.avatar_backend == "soulx_http" and not self.model_worker_token:
            raise ValueError("MODEL_WORKER_TOKEN is required for the SoulX worker")
        if not 1 <= self.max_connections_per_ip <= 100:
            raise ValueError("ECHOWEAVE_MAX_CONNECTIONS_PER_IP is out of range")
        if not 1_024 <= self.max_ws_message_bytes <= 64 * 1024 * 1024:
            raise ValueError("ECHOWEAVE_MAX_WS_MESSAGE_BYTES is out of range")
        if not 1 <= self.max_text_chars <= 10_000:
            raise ValueError("ECHOWEAVE_MAX_TEXT_CHARS is out of range")
        if not 1 <= self.max_utterance_seconds <= 600:
            raise ValueError("ECHOWEAVE_MAX_UTTERANCE_SECONDS is out of range")
        if not 15 <= self.max_session_seconds <= 86_400:
            raise ValueError("ECHOWEAVE_MAX_SESSION_SECONDS is out of range")
        for persona_id in self.allowed_personas:
            if (
                not persona_id
                or persona_id != persona_id.lower()
                or any(
                    char not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                    for char in persona_id
                )
            ):
                raise ValueError(f"invalid allowed persona ID: {persona_id}")
        for origin in self.allowed_origins:
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"invalid allowed origin: {origin}")

    def origin_allowed(self, origin: str | None) -> bool:
        if not origin:
            return True
        local_origins = {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
            f"https://127.0.0.1:{self.port}",
            f"https://localhost:{self.port}",
        }
        return origin in local_origins or origin in self.allowed_origins
