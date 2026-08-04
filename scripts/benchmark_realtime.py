#!/usr/bin/env python3
"""Concurrent EchoWeave WebSocket benchmark with opt-in fault injection.

The client implements the small RFC 6455 subset needed by EchoWeave using only
the Python standard library.  Run against an isolated staging deployment; the
tool creates real model traffic and fault-injection options intentionally send
malformed messages or connection churn.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import random
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import wave
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import SplitResult, urlsplit

try:
    from echoweave.observability import LatencyWindow
    from echoweave.protocol import (
        MAX_MEDIA_PACKET_BYTES,
        MediaPacket,
        PacketKind,
        pack_packet,
        unpack_packet,
    )
except ModuleNotFoundError:  # Allow running from an uninstalled source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from echoweave.observability import LatencyWindow
    from echoweave.protocol import (
        MAX_MEDIA_PACKET_BYTES,
        MediaPacket,
        PacketKind,
        pack_packet,
        unpack_packet,
    )

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HANDSHAKE_BYTES = 64 * 1024
_MAX_MESSAGE_BYTES = MAX_MEDIA_PACKET_BYTES
_AUDIO_SAMPLE_RATE = 16_000
_AUDIO_SAMPLE_WIDTH = 2
_AUDIO_FRAME_MS = 20
_AUDIO_FRAME_BYTES = _AUDIO_SAMPLE_RATE * _AUDIO_SAMPLE_WIDTH * _AUDIO_FRAME_MS // 1_000
_DEFAULT_AUDIO_TAIL_SILENCE_MS = 800
_MAX_AUDIO_WAV_SECONDS = 300
_DEGRADATION_COMPONENTS = frozenset({"avatar", "tts", "turn_cancel"})
_DEGRADATION_FALLBACKS = frozenset(
    {"browser_speech", "generation_quarantine", "static_avatar"}
)


class BenchmarkError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WebSocketClosed(BenchmarkError):
    def __init__(self, close_code: int | None = None) -> None:
        suffix = "unknown" if close_code is None else str(close_code)
        super().__init__(f"websocket_closed_{suffix}")
        self.close_code = close_code


class StandardWebSocket:
    """Minimal synchronous RFC 6455 client for text and binary messages."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float,
        insecure_tls: bool = False,
        max_message_bytes: int = _MAX_MESSAGE_BYTES,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("URL must use ws:// or wss:// and include a host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("URL credentials and fragments are not supported")
        self._parsed = parsed
        self._timeout_seconds = timeout_seconds
        self._insecure_tls = insecure_tls
        self._max_message_bytes = max_message_bytes
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._reader: BinaryIO | None = None
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        if self._socket is not None:
            raise RuntimeError("WebSocket is already connected")
        port = self._parsed.port or (443 if self._parsed.scheme == "wss" else 80)
        raw_socket = socket.create_connection(
            (self._parsed.hostname, port), timeout=self._timeout_seconds
        )
        raw_socket.settimeout(self._timeout_seconds)
        connected_socket: socket.socket | ssl.SSLSocket = raw_socket
        try:
            if self._parsed.scheme == "wss":
                context = (
                    ssl._create_unverified_context()
                    if self._insecure_tls
                    else ssl.create_default_context()
                )
                connected_socket = context.wrap_socket(
                    raw_socket, server_hostname=self._parsed.hostname
                )
                connected_socket.settimeout(self._timeout_seconds)
            self._socket = connected_socket
            self._reader = connected_socket.makefile("rb")
            self._handshake()
        except Exception:
            self.close(send_frame=False)
            raise

    def send_text(self, value: str) -> None:
        self._send_frame(0x1, value.encode("utf-8"))

    def send_binary(self, value: bytes) -> None:
        self._send_frame(0x2, value)

    def receive(self) -> str | bytes:
        message_opcode: int | None = None
        message = bytearray()
        while True:
            final, opcode, payload = self._receive_frame()
            if opcode == 0x8:
                close_code = (
                    struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                )
                raise WebSocketClosed(close_code)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                if message_opcode is not None:
                    raise BenchmarkError("websocket_protocol_error")
                message_opcode = opcode
            elif opcode != 0x0 or message_opcode is None:
                raise BenchmarkError("websocket_protocol_error")
            message.extend(payload)
            if len(message) > self._max_message_bytes:
                raise BenchmarkError("websocket_message_too_large")
            if not final:
                continue
            if message_opcode == 0x1:
                try:
                    return bytes(message).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BenchmarkError("websocket_invalid_utf8") from exc
            return bytes(message)

    def close(self, *, send_frame: bool = True) -> None:
        sock = self._socket
        reader = self._reader
        self._socket = None
        self._reader = None
        if sock is not None and send_frame:
            try:
                self._send_frame_on(sock, 0x8, struct.pack("!H", 1000))
            except OSError:
                pass
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _handshake(self) -> None:
        if self._socket is None or self._reader is None:
            raise RuntimeError("WebSocket is not connected")
        nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = self._parsed.path or "/"
        if self._parsed.query:
            path = f"{path}?{self._parsed.query}"
        host = self._host_header(self._parsed)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._socket.sendall(request)

        status_line = self._read_handshake_line()
        if not status_line.startswith(b"HTTP/1.1 101 "):
            raise BenchmarkError("websocket_handshake_rejected")
        headers: dict[str, str] = {}
        consumed = len(status_line)
        while True:
            line = self._read_handshake_line()
            consumed += len(line)
            if consumed > _MAX_HANDSHAKE_BYTES:
                raise BenchmarkError("websocket_handshake_too_large")
            if line == b"\r\n":
                break
            try:
                name, value = line.decode("ascii").split(":", 1)
            except (UnicodeDecodeError, ValueError) as exc:
                raise BenchmarkError("websocket_invalid_handshake") from exc
            headers[name.strip().lower()] = value.strip()

        expected = base64.b64encode(
            hashlib.sha1((nonce + _WEBSOCKET_GUID).encode("ascii")).digest(),
        ).decode("ascii")
        if not hmac.compare_digest(headers.get("sec-websocket-accept", ""), expected):
            raise BenchmarkError("websocket_invalid_accept")
        if headers.get("upgrade", "").lower() != "websocket":
            raise BenchmarkError("websocket_invalid_upgrade")

    def _read_handshake_line(self) -> bytes:
        if self._reader is None:
            raise RuntimeError("WebSocket is not connected")
        line = self._reader.readline(_MAX_HANDSHAKE_BYTES + 1)
        if not line or len(line) > _MAX_HANDSHAKE_BYTES or not line.endswith(b"\n"):
            raise BenchmarkError("websocket_invalid_handshake")
        return line

    def _receive_frame(self) -> tuple[bool, int, bytes]:
        header = self._read_exact(2)
        first, second = header
        if first & 0x70:
            raise BenchmarkError("websocket_unsupported_extension")
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if opcode >= 0x8 and (not final or length > 125):
            raise BenchmarkError("websocket_invalid_control_frame")
        if length > self._max_message_bytes:
            raise BenchmarkError("websocket_message_too_large")
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if mask is not None:
            payload = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
        return final, opcode, payload

    def _read_exact(self, length: int) -> bytes:
        if self._reader is None:
            raise RuntimeError("WebSocket is not connected")
        result = bytearray()
        while len(result) < length:
            chunk = self._reader.read(length - len(result))
            if not chunk:
                raise WebSocketClosed()
            result.extend(chunk)
        return bytes(result)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._socket is None:
                raise WebSocketClosed()
            self._send_frame_on(self._socket, opcode, payload)

    @staticmethod
    def _send_frame_on(
        sock: socket.socket | ssl.SSLSocket, opcode: int, payload: bytes
    ) -> None:
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sock.sendall(header + mask + masked)

    @staticmethod
    def _host_header(parsed: SplitResult) -> str:
        assert parsed.hostname is not None
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        default_port = 443 if parsed.scheme == "wss" else 80
        return (
            hostname
            if parsed.port in {None, default_port}
            else f"{hostname}:{parsed.port}"
        )


@dataclass(slots=True)
class WorkerResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    injected_disconnects: int = 0
    injected_invalid_messages: int = 0
    degraded_events: int = 0
    connect_ms: list[float] = field(default_factory=list)
    first_token_ms: list[float] = field(default_factory=list)
    text_final_ms: list[float] = field(default_factory=list)
    turn_complete_ms: list[float] = field(default_factory=list)
    speech_end_to_asr_final_ms: list[float] = field(default_factory=list)
    first_audio_ms: list[float] = field(default_factory=list)
    first_video_ms: list[float] = field(default_factory=list)
    audio_packets: int = 0
    audio_bytes: int = 0
    audio_pts_violations: int = 0
    video_packets: int = 0
    video_bytes: int = 0
    video_pts_violations: int = 0
    media_turn_mismatches: int = 0
    degraded_by_component: Counter[str] = field(default_factory=Counter)
    degraded_by_fallback: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class AudioFixture:
    pcm16: bytes
    pcm_sha256: str
    source_duration_ms: float
    stream_duration_ms: int
    frame_count: int
    tail_silence_ms: int


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    url: str
    token: str
    persona: str
    workers: int
    turns: int
    messages: tuple[str, ...]
    timeout_seconds: float
    insecure_tls: bool
    inject_delay_ms: float
    inject_invalid_rate: float
    inject_disconnect_rate: float
    seed: int
    audio: AudioFixture | None = None
    audio_pacing: str = "realtime"


def _load_audio_fixture(
    path: Path,
    *,
    tail_silence_ms: int = _DEFAULT_AUDIO_TAIL_SILENCE_MS,
) -> AudioFixture:
    if tail_silence_ms < 0 or tail_silence_ms > 10_000:
        raise BenchmarkError("audio_tail_silence_invalid")
    if tail_silence_ms % _AUDIO_FRAME_MS:
        raise BenchmarkError("audio_tail_silence_not_frame_aligned")
    if not path.is_file():
        raise BenchmarkError("audio_wav_not_found")
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            compression = stream.getcomptype()
            frame_count = stream.getnframes()
            if channels != 1:
                raise BenchmarkError("audio_wav_not_mono")
            if sample_width != _AUDIO_SAMPLE_WIDTH:
                raise BenchmarkError("audio_wav_not_pcm16")
            if sample_rate != _AUDIO_SAMPLE_RATE:
                raise BenchmarkError("audio_wav_not_16khz")
            if compression != "NONE":
                raise BenchmarkError("audio_wav_compressed")
            if frame_count <= 0:
                raise BenchmarkError("audio_wav_empty")
            if frame_count > _AUDIO_SAMPLE_RATE * _MAX_AUDIO_WAV_SECONDS:
                raise BenchmarkError("audio_wav_too_long")
            pcm16 = stream.readframes(frame_count)
    except BenchmarkError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise BenchmarkError("audio_wav_invalid") from exc
    if len(pcm16) != frame_count * _AUDIO_SAMPLE_WIDTH:
        raise BenchmarkError("audio_wav_truncated")

    source_duration_ms = frame_count * 1_000 / _AUDIO_SAMPLE_RATE
    padding_bytes = (-len(pcm16)) % _AUDIO_FRAME_BYTES
    tail_bytes = tail_silence_ms * _AUDIO_SAMPLE_RATE * _AUDIO_SAMPLE_WIDTH // 1_000
    streamed_pcm16 = pcm16 + (b"\x00" * (padding_bytes + tail_bytes))
    streamed_frame_count = len(streamed_pcm16) // _AUDIO_FRAME_BYTES
    return AudioFixture(
        pcm16=streamed_pcm16,
        pcm_sha256=hashlib.sha256(pcm16).hexdigest(),
        source_duration_ms=source_duration_ms,
        stream_duration_ms=streamed_frame_count * _AUDIO_FRAME_MS,
        frame_count=streamed_frame_count,
        tail_silence_ms=tail_silence_ms,
    )


def _server_message(client: StandardWebSocket) -> dict[str, Any] | MediaPacket:
    message = client.receive()
    if isinstance(message, bytes):
        try:
            packet = unpack_packet(message)
        except (TypeError, ValueError) as exc:
            raise BenchmarkError("server_invalid_media_packet") from exc
        if packet.kind not in {PacketKind.TTS_PCM16, PacketKind.VIDEO_FRAGMENT}:
            raise BenchmarkError("server_unsupported_media_packet")
        return packet
    try:
        payload = json.loads(message)
    except json.JSONDecodeError as exc:
        raise BenchmarkError("server_invalid_json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise BenchmarkError("server_invalid_event")
    return payload


def _event(client: StandardWebSocket) -> dict[str, Any]:
    while True:
        message = _server_message(client)
        if isinstance(message, MediaPacket):
            continue
        return message


def _send_audio(
    client: StandardWebSocket,
    fixture: AudioFixture,
    *,
    pacing: str,
    stop_event: threading.Event,
) -> None:
    if pacing not in {"realtime", "none"}:
        raise BenchmarkError("audio_pacing_invalid")
    started = time.perf_counter()
    for index in range(fixture.frame_count):
        if stop_event.is_set():
            return
        if pacing == "realtime" and index:
            target = started + index * _AUDIO_FRAME_MS / 1_000
            remaining = target - time.perf_counter()
            if remaining > 0 and stop_event.wait(remaining):
                return
        offset = index * _AUDIO_FRAME_BYTES
        frame = fixture.pcm16[offset : offset + _AUDIO_FRAME_BYTES]
        if len(frame) != _AUDIO_FRAME_BYTES:
            raise BenchmarkError("audio_fixture_invalid")
        client.send_binary(
            pack_packet(
                PacketKind.MIC_PCM16,
                turn_id=0,
                pts_ms=index * _AUDIO_FRAME_MS,
                payload=frame,
            )
        )


def _connect(config: BenchmarkConfig, result: WorkerResult) -> StandardWebSocket:
    client = StandardWebSocket(
        config.url,
        timeout_seconds=config.timeout_seconds,
        insecure_tls=config.insecure_tls,
    )
    started = time.perf_counter()
    client.connect()
    try:
        hello = _event(client)
        if hello.get("type") != "session.hello":
            raise BenchmarkError("missing_session_hello")
        client.send_text(
            json.dumps(
                {
                    "type": "start",
                    "access_token": config.token,
                    "persona_id": config.persona,
                    "ai_disclosure_ack": True,
                },
                separators=(",", ":"),
            )
        )
        while True:
            event = _event(client)
            event_type = event["type"]
            if event_type == "session.ready":
                break
            if event_type == "error":
                raise BenchmarkError(_server_error_code(event))
        result.connect_ms.append((time.perf_counter() - started) * 1_000)
        return client
    except Exception:
        client.close()
        raise


def _run_turn(
    client: StandardWebSocket,
    config: BenchmarkConfig,
    result: WorkerResult,
    rng: random.Random,
    message: str,
) -> None:
    started = time.perf_counter()
    if config.inject_delay_ms:
        time.sleep(config.inject_delay_ms / 1_000)
    invalid_injected = rng.random() < config.inject_invalid_rate
    if invalid_injected:
        client.send_text("{")
        result.injected_invalid_messages += 1

    sender_stop = threading.Event()
    sender_errors: list[BaseException] = []
    sender: threading.Thread | None = None
    if config.audio is None:
        client.send_text(
            json.dumps({"type": "text", "text": message}, separators=(",", ":"))
        )
    else:

        def send_audio() -> None:
            try:
                _send_audio(
                    client,
                    config.audio,
                    pacing=config.audio_pacing,
                    stop_event=sender_stop,
                )
            except Exception as exc:  # noqa: BLE001 - propagate thread failures
                sender_errors.append(exc)
                client.close(send_frame=False)

        sender = threading.Thread(
            target=send_audio,
            name="echoweave-benchmark-audio-sender",
            daemon=True,
        )
        sender.start()

    first_token: float | None = None
    text_final: float | None = None
    speech_ended_at: float | None = None
    fatal_error: str | None = None
    last_pts: dict[tuple[PacketKind, int], int] = {}
    seen_media_streams: set[tuple[PacketKind, int]] = set()
    media_turn_ids: list[int] = []
    terminal_turn_id: int | None = None
    terminal_elapsed_ms: float | None = None
    receive_error: BaseException | None = None
    try:
        while terminal_turn_id is None:
            received = _server_message(client)
            received_at = time.perf_counter()
            elapsed_ms = (received_at - started) * 1_000
            if isinstance(received, MediaPacket):
                media_turn_ids.append(received.turn_id)
                pts_key = (received.kind, received.turn_id)
                previous_pts = last_pts.get(pts_key)
                if received.kind == PacketKind.TTS_PCM16:
                    if pts_key not in seen_media_streams:
                        result.first_audio_ms.append(elapsed_ms)
                    result.audio_packets += 1
                    result.audio_bytes += len(received.payload)
                    if previous_pts is not None and received.pts_ms < previous_pts:
                        result.audio_pts_violations += 1
                else:
                    if pts_key not in seen_media_streams:
                        result.first_video_ms.append(elapsed_ms)
                    result.video_packets += 1
                    result.video_bytes += len(received.payload)
                    if previous_pts is not None and received.pts_ms < previous_pts:
                        result.video_pts_violations += 1
                last_pts[pts_key] = received.pts_ms
                seen_media_streams.add(pts_key)
                continue

            event_type = received["type"]
            if event_type == "vad.speech_ended":
                speech_ended_at = received_at
            elif event_type == "asr.final" and speech_ended_at is not None:
                result.speech_end_to_asr_final_ms.append(
                    (received_at - speech_ended_at) * 1_000
                )
            elif event_type == "assistant.delta" and first_token is None:
                first_token = elapsed_ms
            elif event_type == "assistant.final" and text_final is None:
                text_final = elapsed_ms
            elif event_type == "degraded":
                result.degraded_events += 1
                result.degraded_by_component[
                    _bounded_dimension(
                        received.get("component"), _DEGRADATION_COMPONENTS
                    )
                ] += 1
                result.degraded_by_fallback[
                    _bounded_dimension(received.get("fallback"), _DEGRADATION_FALLBACKS)
                ] += 1
            elif event_type == "error":
                code = _server_error_code(received)
                if not (invalid_injected and code == "server_invalid_control_json"):
                    fatal_error = code
            elif (
                event_type == "session.state"
                and received.get("state") == "listening"
                and isinstance(received.get("turn_id"), int)
                and received["turn_id"] > 0
            ):
                terminal_turn_id = received["turn_id"]
                terminal_elapsed_ms = elapsed_ms
    except Exception as exc:  # noqa: BLE001 - preserve receive failure after cleanup
        receive_error = exc
    finally:
        sender_stop.set()
        if sender is not None:
            sender.join(timeout=5.0)

    if sender is not None and sender.is_alive():
        raise BenchmarkError("audio_sender_stuck")
    if sender_errors:
        error = sender_errors[0]
        if isinstance(error, BenchmarkError):
            raise error
        raise BenchmarkError("audio_send_failed") from error
    if receive_error is not None:
        raise receive_error
    if terminal_turn_id is None or terminal_elapsed_ms is None:
        raise BenchmarkError("turn_incomplete")
    result.media_turn_mismatches += sum(
        turn_id != terminal_turn_id for turn_id in media_turn_ids
    )
    if fatal_error is not None:
        raise BenchmarkError(fatal_error)
    if first_token is None or text_final is None:
        raise BenchmarkError("turn_incomplete")
    result.first_token_ms.append(first_token)
    result.text_final_ms.append(text_final)
    result.turn_complete_ms.append(terminal_elapsed_ms)


def _safe_dimension(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    safe = "".join(
        character
        for character in value.lower()
        if character.isalnum() or character in {"_", "-"}
    )
    return safe[:80] or "unknown"


def _bounded_dimension(value: object, allowed: frozenset[str]) -> str:
    normalized = _safe_dimension(value)
    return normalized if normalized in allowed else "unknown"


def _server_error_code(event: Mapping[str, Any]) -> str:
    raw_code = event.get("code")
    if not isinstance(raw_code, str) or not raw_code:
        return "server_error_unknown"
    safe = _safe_dimension(raw_code).replace("-", "_")
    return f"server_{safe}" if safe != "unknown" else "server_error_unknown"


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, BenchmarkError):
        return exc.code
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, OSError):
        return "network_error"
    return "client_error"


def _worker(
    index: int, config: BenchmarkConfig, start_barrier: threading.Barrier
) -> WorkerResult:
    result = WorkerResult()
    rng = random.Random(config.seed + index * 1_000_003)
    client: StandardWebSocket | None = None
    start_barrier.wait()
    try:
        for turn_index in range(config.turns):
            result.attempted += 1
            try:
                if client is None:
                    client = _connect(config, result)
                if rng.random() < config.inject_disconnect_rate:
                    client.close()
                    client = None
                    result.injected_disconnects += 1
                    client = _connect(config, result)
                message = config.messages[
                    (index * config.turns + turn_index) % len(config.messages)
                ]
                _run_turn(client, config, result, rng, message)
                result.succeeded += 1
            except Exception as exc:  # noqa: BLE001 - benchmark must aggregate failures
                result.failed += 1
                result.errors[_error_code(exc)] += 1
                if client is not None:
                    client.close()
                    client = None
    finally:
        if client is not None:
            try:
                client.send_text('{"type":"stop"}')
            except (BenchmarkError, OSError):
                pass
            client.close()
    return result


def _latency_summary(values: list[float]) -> dict[str, int | float | None]:
    window = LatencyWindow(max(1, len(values)))
    for value in values:
        window.observe(value)
    return window.snapshot()


def _combined_counter(results: list[WorkerResult], attribute: str) -> Counter[str]:
    combined: Counter[str] = Counter()
    for result in results:
        combined.update(getattr(result, attribute))
    return combined


def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    barrier = threading.Barrier(config.workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.workers) as executor:
        futures = [
            executor.submit(_worker, index, config, barrier)
            for index in range(config.workers)
        ]
        results = [future.result() for future in futures]
    duration_seconds = max(0.000_001, time.perf_counter() - started)

    errors: Counter[str] = Counter()
    for result in results:
        errors.update(result.errors)
    attempted = sum(result.attempted for result in results)
    succeeded = sum(result.succeeded for result in results)
    failed = sum(result.failed for result in results)
    audio_packets = sum(result.audio_packets for result in results)
    audio_pts_violations = sum(result.audio_pts_violations for result in results)
    video_packets = sum(result.video_packets for result in results)
    video_pts_violations = sum(result.video_pts_violations for result in results)
    degraded_by_component = _combined_counter(results, "degraded_by_component")
    degraded_by_fallback = _combined_counter(results, "degraded_by_fallback")

    input_config: dict[str, Any] = {
        "mode": "audio_wav" if config.audio is not None else "text",
    }
    if config.audio is not None:
        input_config["audio"] = {
            "pcm_sha256": config.audio.pcm_sha256,
            "source_duration_ms": round(config.audio.source_duration_ms, 3),
            "stream_duration_ms": config.audio.stream_duration_ms,
            "frame_duration_ms": _AUDIO_FRAME_MS,
            "frame_count": config.audio.frame_count,
            "tail_silence_ms": config.audio.tail_silence_ms,
            "pacing": config.audio_pacing,
        }

    return {
        "schema_version": 2,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "duration_seconds": duration_seconds,
        "config": {
            "workers": config.workers,
            "turns_per_worker": config.turns,
            "timeout_seconds": config.timeout_seconds,
            "insecure_tls": config.insecure_tls,
            "input": input_config,
            "fault_injection": {
                "delay_ms": config.inject_delay_ms,
                "invalid_message_rate": config.inject_invalid_rate,
                "disconnect_rate": config.inject_disconnect_rate,
                "seed": config.seed,
            },
        },
        "totals": {
            "attempted_turns": attempted,
            "successful_turns": succeeded,
            "failed_turns": failed,
            "success_rate": succeeded / attempted if attempted else 0.0,
            "turns_per_second": succeeded / duration_seconds,
            "connections": sum(len(result.connect_ms) for result in results),
            "degraded_events": sum(result.degraded_events for result in results),
            "injected_disconnects": sum(
                result.injected_disconnects for result in results
            ),
            "injected_invalid_messages": sum(
                result.injected_invalid_messages for result in results
            ),
        },
        "latency_ms": {
            "connect_ready": _latency_summary(
                [value for result in results for value in result.connect_ms]
            ),
            "first_token": _latency_summary(
                [value for result in results for value in result.first_token_ms]
            ),
            "text_final": _latency_summary(
                [value for result in results for value in result.text_final_ms]
            ),
            "turn_complete": _latency_summary(
                [value for result in results for value in result.turn_complete_ms]
            ),
            "speech_end_to_asr_final": _latency_summary(
                [
                    value
                    for result in results
                    for value in result.speech_end_to_asr_final_ms
                ]
            ),
            "first_audio": _latency_summary(
                [value for result in results for value in result.first_audio_ms]
            ),
            "first_video": _latency_summary(
                [value for result in results for value in result.first_video_ms]
            ),
        },
        "media": {
            "audio": {
                "packets": audio_packets,
                "bytes": sum(result.audio_bytes for result in results),
                "pts_monotonic": (audio_pts_violations == 0 if audio_packets else None),
                "pts_violations": audio_pts_violations,
            },
            "video": {
                "packets": video_packets,
                "bytes": sum(result.video_bytes for result in results),
                "pts_monotonic": (video_pts_violations == 0 if video_packets else None),
                "pts_violations": video_pts_violations,
            },
            "turn_id_mismatches": sum(
                result.media_turn_mismatches for result in results
            ),
        },
        "degradations": {
            "total_events": sum(result.degraded_events for result in results),
            "by_component": dict(sorted(degraded_by_component.items())),
            "by_fallback": dict(sorted(degraded_by_fallback.items())),
        },
        "errors": dict(sorted(errors.items())),
    }


def _rate(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("rate must be between 0 and 1")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed < float("inf"):
        raise argparse.ArgumentTypeError("value must be finite and non-negative")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument(
        "--token",
        default=os.getenv("ECHOWEAVE_ACCESS_TOKEN", ""),
        help="session token; defaults to ECHOWEAVE_ACCESS_TOKEN and is never reported",
    )
    parser.add_argument("--persona", default="demo")
    parser.add_argument("--workers", type=_positive_int, default=1)
    parser.add_argument("--turns", type=_positive_int, default=5)
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--message",
        action="append",
        dest="messages",
        help="text turn; repeat for a round-robin corpus",
    )
    input_group.add_argument(
        "--audio-wav",
        type=Path,
        help="repeat one mono 16 kHz PCM16 WAV as a paced microphone turn",
    )
    parser.add_argument(
        "--audio-pacing",
        choices=("realtime", "none"),
        default="realtime",
        help="pace WAV frames in realtime (default) or send without pacing",
    )
    parser.add_argument(
        "--audio-tail-silence-ms",
        type=_nonnegative_int,
        default=_DEFAULT_AUDIO_TAIL_SILENCE_MS,
        help="append frame-aligned silence so VAD can close the utterance",
    )
    parser.add_argument("--timeout-seconds", type=_nonnegative_float, default=60.0)
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="disable TLS certificate verification (staging only)",
    )
    parser.add_argument("--inject-delay-ms", type=_nonnegative_float, default=0.0)
    parser.add_argument("--inject-invalid-rate", type=_rate, default=0.0)
    parser.add_argument("--inject-disconnect-rate", type=_rate, default=0.0)
    parser.add_argument("--seed", type=int, default=20260801)
    parser.add_argument("--min-success-rate", type=_rate, default=0.99)
    parser.add_argument(
        "--output", type=Path, help="also write the JSON report to this file"
    )
    args = parser.parse_args(argv)
    if args.workers > 256:
        parser.error("--workers may not exceed 256")
    if args.turns > 100:
        parser.error("--turns may not exceed the gateway's 100-turn session limit")
    if not 0 < args.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be greater than zero and at most 600")
    if args.inject_delay_ms > 60_000:
        parser.error("--inject-delay-ms may not exceed 60000")
    if args.audio_tail_silence_ms > 10_000:
        parser.error("--audio-tail-silence-ms may not exceed 10000")
    if args.audio_tail_silence_ms % _AUDIO_FRAME_MS:
        parser.error(f"--audio-tail-silence-ms must be a multiple of {_AUDIO_FRAME_MS}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    messages = tuple(args.messages or ("请用一句话说明你是一个 AI 数字人。",))
    if any(not message or len(message) > 10_000 for message in messages):
        raise SystemExit("messages must contain 1 to 10000 characters")
    try:
        audio = (
            _load_audio_fixture(
                args.audio_wav,
                tail_silence_ms=args.audio_tail_silence_ms,
            )
            if args.audio_wav is not None
            else None
        )
        config = BenchmarkConfig(
            url=args.url,
            token=args.token,
            persona=args.persona,
            workers=args.workers,
            turns=args.turns,
            messages=messages,
            timeout_seconds=args.timeout_seconds,
            insecure_tls=args.insecure_tls,
            inject_delay_ms=args.inject_delay_ms,
            inject_invalid_rate=args.inject_invalid_rate,
            inject_disconnect_rate=args.inject_disconnect_rate,
            seed=args.seed,
            audio=audio,
            audio_pacing=args.audio_pacing,
        )
        report = run_benchmark(config)
    except (ValueError, OSError, BenchmarkError) as exc:
        print(json.dumps({"fatal_error": _error_code(exc)}, separators=(",", ":")))
        return 2
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["totals"]["success_rate"] >= args.min_success_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())
