from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from echoweave import __version__
from echoweave.config import Settings
from echoweave.persona import PersonaConsentError, PersonaRegistry
from echoweave.pipeline import RealtimeSession
from echoweave.protocol import PacketKind, unpack_packet
from echoweave.runtime import build_runtime

LOGGER = logging.getLogger("echoweave")
WEB_ROOT = Path(__file__).with_name("web")
MIC_SAMPLE_RATE = 16_000
MIC_BYTES_PER_SAMPLE = 2
MIN_MIC_FRAME_MS = 10
MAX_MIC_FRAME_MS = 250
MIN_MIC_PAYLOAD_BYTES = (
    MIC_SAMPLE_RATE * MIC_BYTES_PER_SAMPLE * MIN_MIC_FRAME_MS // 1_000
)
MAX_MIC_PAYLOAD_BYTES = (
    MIC_SAMPLE_RATE * MIC_BYTES_PER_SAMPLE * MAX_MIC_FRAME_MS // 1_000
)
MIC_RATE_BURST_SECONDS = 1.0
MIC_RATE_REFILL_RATIO = 1.25


MODEL_SOURCES = {
    "silero_vad_v5": "https://github.com/snakers4/silero-vad/tree/v5.1.2",
    "qwen3_asr_1_7b": "https://huggingface.co/Qwen/Qwen3-ASR-1.7B",
    "deepseek_v4_flash": "https://api-docs.deepseek.com/",
    "voxcpm2": "https://huggingface.co/openbmb/VoxCPM2",
    "nuwa_skill": "https://github.com/alchaincyf/nuwa-skill",
    "soulx_flashhead": "https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B",
}


def _is_loopback_host(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower().split("%", 1)[0]
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _host_header_name(value: str | None) -> str:
    if not value:
        return ""
    try:
        return urlsplit(f"//{value}", allow_fragments=False).hostname or ""
    except ValueError:
        return ""


def _tokenless_connection_is_local(websocket: WebSocket) -> bool:
    client_host = websocket.client.host if websocket.client else ""
    server = websocket.scope.get("server")
    server_host = str(server[0]) if server else ""
    requested_host = _host_header_name(websocket.headers.get("host"))
    return all(
        _is_loopback_host(host) for host in (client_host, server_host, requested_host)
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    app = FastAPI(
        title="EchoWeave-RTC",
        version=__version__,
        description="Consent-first realtime voice and avatar agent gateway.",
    )
    registry = PersonaRegistry(
        root=settings.persona_root,
        signing_key=settings.consent_signing_key,
        require_third_party_scope=settings.llm_backend == "deepseek",
        state_path=(
            settings.consent_state_path if settings.consent_signing_key else None
        ),
    )
    active_connections: dict[str, int] = {}
    audio_rate_buckets: dict[str, tuple[float, float]] = {}
    connection_lock = asyncio.Lock()

    async def consume_audio_budget(client_host: str, duration_seconds: float) -> bool:
        now = time.monotonic()
        async with connection_lock:
            budget, updated_at = audio_rate_buckets.get(
                client_host,
                (MIC_RATE_BURST_SECONDS, now),
            )
            budget = min(
                MIC_RATE_BURST_SECONDS,
                budget + (now - updated_at) * MIC_RATE_REFILL_RATIO,
            )
            allowed = duration_seconds <= budget
            if allowed:
                budget -= duration_seconds
            audio_rate_buckets[client_host] = (budget, now)
            return allowed

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "microphone=(self), camera=(), geolocation=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        return response

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "EchoWeave-RTC",
                "version": __version__,
            }
        )

    @app.get("/api/model-sources")
    async def model_sources() -> JSONResponse:
        return JSONResponse(MODEL_SOURCES)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        if not settings.access_token and not _tokenless_connection_is_local(websocket):
            await websocket.close(
                code=1008,
                reason="access token required for non-loopback connections",
            )
            return
        origin = websocket.headers.get("origin")
        if not settings.origin_allowed(origin):
            await websocket.close(code=1008, reason="origin rejected")
            return
        client_host = websocket.client.host if websocket.client else "unknown"
        async with connection_lock:
            now = time.monotonic()
            if len(audio_rate_buckets) > 4_096:
                stale_hosts = [
                    host
                    for host, (_, updated_at) in audio_rate_buckets.items()
                    if host not in active_connections and now - updated_at > 120
                ]
                for host in stale_hosts:
                    audio_rate_buckets.pop(host, None)
            connection_count = active_connections.get(client_host, 0)
            if connection_count >= settings.max_connections_per_ip:
                await websocket.close(code=1013, reason="connection limit reached")
                return
            active_connections[client_host] = connection_count + 1
            audio_rate_buckets.setdefault(
                client_host,
                (MIC_RATE_BURST_SECONDS, now),
            )
        session_id = uuid.uuid4().hex
        connected_at = time.monotonic()
        start_deadline = connected_at + 15.0
        start_attempts = 0
        send_lock = asyncio.Lock()
        session: RealtimeSession | None = None

        async def emit_json(payload: dict) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def emit_binary(payload: bytes) -> None:
            async with send_lock:
                await websocket.send_bytes(payload)

        try:
            await websocket.accept()
            await emit_json(
                {
                    "type": "session.hello",
                    "session_id": session_id,
                    "protocol": {
                        "magic": "EW",
                        "version": 1,
                        "audio_input_hz": 16000,
                    },
                    "requires_ai_disclosure_ack": True,
                }
            )
            while True:
                if time.monotonic() - connected_at > settings.max_session_seconds:
                    await emit_json(
                        {
                            "type": "error",
                            "code": "session_expired",
                            "message": "session duration limit reached",
                        }
                    )
                    break
                remaining = settings.max_session_seconds - (
                    time.monotonic() - connected_at
                )
                if session is None:
                    start_remaining = start_deadline - time.monotonic()
                    if start_remaining <= 0:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "start_timeout",
                                "message": "session start deadline reached",
                            }
                        )
                        break
                    receive_timeout = min(start_remaining, remaining)
                else:
                    receive_timeout = min(60.0, remaining)
                try:
                    message = await asyncio.wait_for(
                        websocket.receive(),
                        timeout=max(0.1, receive_timeout),
                    )
                except TimeoutError:
                    if session is None:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "start_timeout",
                                "message": "session start deadline reached",
                            }
                        )
                        break
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if session is None:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "session_not_started",
                                "message": "send a start control message first",
                            }
                        )
                        await websocket.close(
                            code=1008,
                            reason="media sent before session start",
                        )
                        break
                    raw_packet = message["bytes"]
                    if len(raw_packet) > settings.max_ws_message_bytes:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "media_packet_too_large",
                                "message": "media packet exceeds the configured limit",
                            }
                        )
                        await websocket.close(
                            code=1008, reason="media packet too large"
                        )
                        break
                    try:
                        packet = unpack_packet(raw_packet)
                    except ValueError as exc:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "invalid_media_packet",
                                "message": str(exc),
                            }
                        )
                        await websocket.close(code=1008, reason="invalid media packet")
                        break
                    if packet.kind != PacketKind.MIC_PCM16:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "unsupported_client_media",
                                "message": "clients may only send microphone PCM",
                            }
                        )
                        await websocket.close(
                            code=1008,
                            reason="unsupported client media",
                        )
                        break
                    if (
                        len(packet.payload) < MIN_MIC_PAYLOAD_BYTES
                        or len(packet.payload) > MAX_MIC_PAYLOAD_BYTES
                        or len(packet.payload) % MIC_BYTES_PER_SAMPLE
                    ):
                        await emit_json(
                            {
                                "type": "error",
                                "code": "invalid_mic_frame",
                                "message": (
                                    f"microphone PCM frames must be {MIN_MIC_FRAME_MS}-"
                                    f"{MAX_MIC_FRAME_MS} ms of 16 kHz PCM16"
                                ),
                            }
                        )
                        await websocket.close(code=1008, reason="invalid mic frame")
                        break
                    duration_seconds = len(packet.payload) / (
                        MIC_SAMPLE_RATE * MIC_BYTES_PER_SAMPLE
                    )
                    if not await consume_audio_budget(
                        client_host,
                        duration_seconds,
                    ):
                        await emit_json(
                            {
                                "type": "error",
                                "code": "mic_rate_exceeded",
                                "message": "microphone audio exceeded realtime rate",
                            }
                        )
                        await websocket.close(
                            code=1008,
                            reason="microphone rate exceeded",
                        )
                        break
                    await session.ingest_pcm(packet.payload, MIC_SAMPLE_RATE)
                    continue

                raw_text = message.get("text")
                if raw_text is None:
                    continue
                if len(raw_text.encode("utf-8")) > settings.max_ws_message_bytes:
                    await emit_json(
                        {
                            "type": "error",
                            "code": "control_message_too_large",
                            "message": "control message exceeds the configured limit",
                        }
                    )
                    continue
                try:
                    control = json.loads(raw_text)
                except json.JSONDecodeError:
                    await emit_json(
                        {
                            "type": "error",
                            "code": "invalid_control_json",
                            "message": "control messages must be JSON",
                        }
                    )
                    continue
                if not isinstance(control, dict):
                    await emit_json(
                        {
                            "type": "error",
                            "code": "invalid_control_json",
                            "message": "control messages must be JSON objects",
                        }
                    )
                    continue
                action = control.get("type")
                if action == "start":
                    if session is not None:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "session_already_started",
                                "message": "open a new connection to change persona",
                            }
                        )
                        continue
                    start_attempts += 1
                    if start_attempts > 3:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "start_attempt_limit",
                                "message": "session start attempt limit reached",
                            }
                        )
                        await websocket.close(
                            code=1008,
                            reason="session start attempt limit",
                        )
                        break
                    raw_token = control.get("access_token", "")
                    supplied_token = raw_token if isinstance(raw_token, str) else ""
                    if settings.access_token and not hmac.compare_digest(
                        supplied_token, settings.access_token
                    ):
                        await emit_json(
                            {
                                "type": "error",
                                "code": "authentication_failed",
                                "message": "session token rejected",
                            }
                        )
                        continue
                    if control.get("ai_disclosure_ack") is not True:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "disclosure_not_acknowledged",
                                "message": "AI identity disclosure must be acknowledged",
                            }
                        )
                        continue
                    try:
                        raw_persona_id = control.get("persona_id", "demo")
                        if not isinstance(raw_persona_id, str):
                            raise PersonaConsentError("persona ID must be a string")
                        persona_id = raw_persona_id
                        if persona_id not in settings.allowed_personas:
                            raise PersonaConsentError(
                                "persona is not allowed by this deployment"
                            )
                        persona = registry.load(persona_id)
                        adapters = build_runtime(settings)
                    except (PersonaConsentError, RuntimeError, ValueError) as exc:
                        reference = uuid.uuid4().hex[:12]
                        LOGGER.warning(
                            "session start rejected reference=%s type=%s",
                            reference,
                            type(exc).__name__,
                        )
                        await emit_json(
                            {
                                "type": "error",
                                "code": "session_start_rejected",
                                "message": f"persona authorization rejected ({reference})",
                            }
                        )
                        continue
                    session = RealtimeSession(
                        session_id,
                        persona,
                        adapters,
                        emit_json,
                        emit_binary,
                        authorization_check=persona.revalidate,
                        max_utterance_seconds=settings.max_utterance_seconds,
                        max_text_chars=settings.max_text_chars,
                    )
                    await session.start()
                elif action == "text" and session is not None:
                    text = control.get("text", "")
                    if not isinstance(text, str):
                        await emit_json(
                            {
                                "type": "error",
                                "code": "invalid_text",
                                "message": "text turn must contain a string",
                            }
                        )
                        continue
                    if len(text) > settings.max_text_chars:
                        await emit_json(
                            {
                                "type": "error",
                                "code": "text_too_long",
                                "message": "text turn exceeds the configured limit",
                            }
                        )
                    else:
                        await session.submit_text(text)
                elif action == "cancel" and session is not None:
                    await session.cancel_response("client_cancelled")
                elif action == "stop":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            LOGGER.exception("websocket session failed")
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
            async with connection_lock:
                remaining_connections = active_connections.get(client_host, 1) - 1
                if remaining_connections > 0:
                    active_connections[client_host] = remaining_connections
                else:
                    active_connections.pop(client_host, None)

    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
    return app


app = create_app()
