"""Authenticated, bounded streaming worker for the official VoxCPM2 API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import importlib.util
import logging
import os
import queue
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER = logging.getLogger("echoweave.voxcpm_worker")

VOXCPM_MODEL = os.environ.get("VOXCPM_MODEL", "openbmb/VoxCPM2")
MAX_TEXT_CHARS = int(os.environ.get("VOXCPM_MAX_TEXT_CHARS", "2000"))
MAX_PROMPT_TEXT_CHARS = int(os.environ.get("VOXCPM_MAX_PROMPT_TEXT_CHARS", "2000"))
MAX_REFERENCE_AUDIO_BYTES = int(
    os.environ.get("VOXCPM_MAX_REFERENCE_AUDIO_BYTES", "33554432")
)
MAX_OUTPUT_BYTES = int(os.environ.get("VOXCPM_MAX_OUTPUT_BYTES", "268435456"))
MAX_ENCODED_REFERENCE_BYTES = ((MAX_REFERENCE_AUDIO_BYTES + 2) // 3) * 4
MAX_REQUEST_BYTES = MAX_ENCODED_REFERENCE_BYTES + 2 * 1024 * 1024

INFERENCE_LOCK = threading.Lock()
MODEL_LOAD_LOCK = threading.Lock()
MODEL = None

app = FastAPI(
    title="EchoWeave VoxCPM2 Worker",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


async def _await_cleanup(finalizer: Callable[[], Awaitable[None]]) -> None:
    """Finish cleanup even when the request task is already being cancelled."""

    cleanup_task = asyncio.create_task(finalizer())
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError:
            if cleanup_task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class _FinalizingStreamingResponse(StreamingResponse):
    """Own a resource lifecycle across the complete ASGI response call.

    Starlette does not run a ``BackgroundTask`` when sending
    ``http.response.start`` itself fails.  Keeping the finalizer outside the
    base response also gives ASGI 2.3 disconnect cancellation a second,
    uncancelled place to wait for the model producer.
    """

    def __init__(
        self,
        *args,
        finalizer: Callable[[], Awaitable[None]],
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._finalizer = finalizer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _await_cleanup(self._finalizer)


class SpeechRequest(BaseModel):
    model: str = VOXCPM_MODEL
    input: str
    voice: str = "default"
    response_format: str = "pcm"
    stream: bool = True
    stream_format: str = "audio"
    ref_audio: str | None = None
    ref_text: str | None = None


def _configured_worker_token() -> str:
    token = os.environ.get("MODEL_WORKER_TOKEN", "").strip()
    return token if len(token.encode("utf-8")) >= 32 else ""


def _tokens_match(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(
        supplied.encode("utf-8"),
        expected.encode("utf-8"),
    )


def _require_worker_token(
    authorization: Annotated[str | None, Header()] = None,
    x_worker_token: Annotated[str | None, Header()] = None,
    x_echoweave_worker_token: Annotated[
        str | None,
        Header(alias="X-EchoWeave-Worker-Token"),
    ] = None,
) -> None:
    expected = _configured_worker_token()
    if not expected:
        raise HTTPException(
            status_code=503, detail="Worker authentication is unavailable."
        )
    supplied = x_worker_token or x_echoweave_worker_token or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not _tokens_match(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Worker authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _scope_header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


def _scope_worker_token(scope: Scope) -> str:
    current = _scope_header_values(scope, b"x-worker-token")
    legacy = _scope_header_values(scope, b"x-echoweave-worker-token")
    authorization = _scope_header_values(scope, b"authorization")
    if len(current) > 1 or len(legacy) > 1 or len(authorization) > 1:
        return ""
    if current:
        return current[0].strip()
    if legacy:
        return legacy[0].strip()
    if authorization and authorization[0].lower().startswith("bearer "):
        return authorization[0][7:].strip()
    return ""


async def _guard_response(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    detail: str,
) -> None:
    headers = {"Cache-Control": "no-store"}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )
    await response(scope, receive, send)


class _BoundedAuthenticatedBody:
    """Authenticate before reading, then buffer at most one bounded request body."""

    def __init__(self, app: ASGIApp, route_limits: dict[str, int]) -> None:
        self.app = app
        self.route_limits = route_limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.route_limits:
            await self.app(scope, receive, send)
            return

        expected = _configured_worker_token()
        if not expected:
            await _guard_response(
                scope,
                receive,
                send,
                503,
                "Worker authentication is unavailable.",
            )
            return
        supplied = _scope_worker_token(scope)
        if not supplied or not _tokens_match(supplied, expected):
            await _guard_response(
                scope,
                receive,
                send,
                401,
                "Worker authentication failed.",
            )
            return

        limit = self.route_limits[scope["path"]]
        content_lengths = _scope_header_values(scope, b"content-length")
        if len(content_lengths) > 1:
            await _guard_response(
                scope,
                receive,
                send,
                400,
                "Invalid Content-Length header.",
            )
            return
        if content_lengths:
            try:
                declared_length = int(content_lengths[0])
            except ValueError:
                declared_length = -1
            if declared_length < 0:
                await _guard_response(
                    scope,
                    receive,
                    send,
                    400,
                    "Invalid Content-Length header.",
                )
                return
            if declared_length > limit:
                await _guard_response(
                    scope,
                    receive,
                    send,
                    413,
                    "Request body is too large.",
                )
                return

        buffered: list[Message] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > limit:
                await _guard_response(
                    scope,
                    receive,
                    send,
                    413,
                    "Request body is too large.",
                )
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        position = 0

        async def replay_receive() -> Message:
            nonlocal position
            if position < len(buffered):
                message = buffered[position]
                position += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


app.add_middleware(
    _BoundedAuthenticatedBody,
    route_limits={
        "/v1/audio/speech": MAX_REQUEST_BYTES,
        "/v1/audio/speech/clone": MAX_REQUEST_BYTES,
    },
)


def _dependency_status() -> dict[str, bool]:
    checks = {
        "voxcpm": importlib.util.find_spec("voxcpm") is not None,
        "numpy": importlib.util.find_spec("numpy") is not None,
        "torch": importlib.util.find_spec("torch") is not None,
        "authentication": bool(_configured_worker_token()),
    }
    try:
        import torch

        checks["cuda"] = bool(torch.cuda.is_available())
    except (ImportError, RuntimeError):
        checks["cuda"] = False
    return checks


@app.get("/health")
async def health() -> JSONResponse:
    checks = _dependency_status()
    return JSONResponse(
        {
            "ok": all(checks.values()),
            "checks": checks,
            "model_loaded": MODEL is not None,
            "synthetic_voice": True,
        },
        headers={"Cache-Control": "no-store"},
    )


def _load_model():
    try:
        from voxcpm import VoxCPM
    except ImportError as exc:
        raise RuntimeError("VoxCPM2 runtime is unavailable.") from exc
    return VoxCPM.from_pretrained(VOXCPM_MODEL, load_denoiser=False)


def _get_model():
    global MODEL
    if MODEL is not None:
        return MODEL
    with MODEL_LOAD_LOCK:
        if MODEL is None:
            MODEL = _load_model()
    return MODEL


def _validate_text(text: str, limit: int, label: str) -> str:
    normalized = text.strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise HTTPException(status_code=400, detail=f"{label} is invalid.")
    return normalized


async def _save_reference_audio(upload: UploadFile, destination: Path) -> None:
    size = 0
    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_REFERENCE_AUDIO_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Reference audio is too large.",
                    )
                output.write(chunk)
    finally:
        await upload.close()
    if size == 0:
        raise HTTPException(status_code=400, detail="Reference audio is empty.")
    header = destination.read_bytes()[:12]
    if len(header) < 12 or not (header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
        raise HTTPException(
            status_code=415,
            detail="Reference audio must be a RIFF/WAVE file.",
        )


def _decode_reference_data_uri(data_uri: str, workdir: Path) -> Path:
    metadata, separator, encoded = data_uri.partition(",")
    lowered = metadata.lower()
    if (
        not separator
        or not lowered.startswith("data:audio/")
        or not lowered.endswith(";base64")
    ):
        raise HTTPException(
            status_code=400,
            detail="Reference audio must be an audio base64 data URI.",
        )
    mime_type = lowered[5:-7]
    supported = {
        "audio/wav": (
            ".wav",
            lambda data: data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        ),
        "audio/x-wav": (
            ".wav",
            lambda data: data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        ),
        "audio/wave": (
            ".wav",
            lambda data: data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        ),
        "audio/flac": (".flac", lambda data: data[:4] == b"fLaC"),
        "audio/ogg": (".ogg", lambda data: data[:4] == b"OggS"),
        "audio/mpeg": (
            ".mp3",
            lambda data: (
                data[:3] == b"ID3"
                or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
            ),
        ),
        "audio/mp3": (
            ".mp3",
            lambda data: (
                data[:3] == b"ID3"
                or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
            ),
        ),
    }
    media = supported.get(mime_type)
    if media is None:
        raise HTTPException(
            status_code=415,
            detail="Reference audio data URI has an unsupported media type.",
        )
    if len(encoded) > MAX_ENCODED_REFERENCE_BYTES + 4:
        raise HTTPException(status_code=413, detail="Reference audio is too large.")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Reference audio data URI is malformed.",
        ) from exc
    if not payload:
        raise HTTPException(status_code=400, detail="Reference audio is empty.")
    if len(payload) > MAX_REFERENCE_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Reference audio is too large.")
    suffix, signature_check = media
    if len(payload) < 12 or not signature_check(payload):
        raise HTTPException(
            status_code=415,
            detail="Reference audio does not match its declared media type.",
        )
    destination = workdir / f"reference{suffix}"
    with destination.open("xb") as output:
        output.write(payload)
    return destination


def _queue_producer_item(
    output_queue: queue.Queue[tuple[str, bytes | None]],
    stop_event: threading.Event,
    item: tuple[str, bytes | None],
) -> bool:
    while not stop_event.is_set():
        try:
            output_queue.put(item, timeout=0.1)
        except queue.Full:
            continue
        return True
    return False


class _VoxStreamLifecycle:
    """Idempotently stop one producer before deleting its private inputs."""

    def __init__(self, cleanup_root: Path | None) -> None:
        self.cleanup_root = cleanup_root
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._producer: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._producer_started = False
        self._closed = False

    def start(
        self,
        producer: threading.Thread,
        stop_event: threading.Event,
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("VoxCPM2 response lifecycle is already closed.")
            self._producer = producer
            self._stop_event = stop_event
            producer.start()
            self._producer_started = True

    def request_stop(self) -> None:
        with self._state_lock:
            stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()

    def _close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return
                producer = self._producer
                producer_started = self._producer_started
                stop_event = self._stop_event
            if stop_event is not None:
                stop_event.set()
            if producer is not None and producer_started:
                producer.join()
            if self.cleanup_root is not None:
                shutil.rmtree(self.cleanup_root, ignore_errors=True)
            with self._state_lock:
                self._closed = True

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close)


def _produce_pcm(
    text: str,
    reference_path: Path | None,
    prompt_text: str | None,
    output_queue: queue.Queue[tuple[str, bytes | None]],
    stop_event: threading.Event,
) -> None:
    iterator = None
    try:
        import numpy as np

        with INFERENCE_LOCK:
            try:
                model = _get_model()
                kwargs: dict[str, object] = {"text": text}
                if reference_path is not None:
                    kwargs["reference_wav_path"] = str(reference_path)
                    if prompt_text:
                        kwargs["prompt_wav_path"] = str(reference_path)
                        kwargs["prompt_text"] = prompt_text
                iterator = iter(model.generate_streaming(**kwargs))
                bytes_sent = 0
                for chunk in iterator:
                    if stop_event.is_set():
                        break
                    values = np.asarray(chunk, dtype=np.float32).reshape(-1)
                    if not np.isfinite(values).all():
                        values = np.nan_to_num(
                            values,
                            nan=0.0,
                            posinf=1.0,
                            neginf=-1.0,
                        )
                    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    bytes_sent += len(pcm)
                    if bytes_sent > MAX_OUTPUT_BYTES:
                        raise RuntimeError("VoxCPM2 output exceeded the size limit.")
                    if pcm and not _queue_producer_item(
                        output_queue,
                        stop_event,
                        ("audio", pcm),
                    ):
                        break
                if not stop_event.is_set():
                    _queue_producer_item(output_queue, stop_event, ("done", None))
            finally:
                if iterator is not None and hasattr(iterator, "close"):
                    iterator.close()
    except BaseException:
        LOGGER.exception("VoxCPM2 generation failed.")
        if not stop_event.is_set():
            _queue_producer_item(output_queue, stop_event, ("error", None))


async def _pcm_stream(
    text: str,
    reference_path: Path | None,
    prompt_text: str | None,
    lifecycle: _VoxStreamLifecycle,
) -> AsyncIterator[bytes]:
    output_queue: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=4)
    stop_event = threading.Event()
    producer = threading.Thread(
        target=_produce_pcm,
        args=(
            text,
            reference_path,
            prompt_text,
            output_queue,
            stop_event,
        ),
        name="echoweave-voxcpm-inference",
        daemon=False,
    )
    try:
        lifecycle.start(producer, stop_event)
        while True:
            try:
                message_type, payload = output_queue.get_nowait()
            except queue.Empty:
                if not producer.is_alive():
                    raise RuntimeError("VoxCPM2 producer exited unexpectedly.")
                await asyncio.sleep(0.01)
                continue
            if message_type == "audio":
                if payload:
                    yield payload
                continue
            if message_type == "done":
                break
            raise RuntimeError("VoxCPM2 generation failed.")
    finally:
        # The response-level finalizer owns the blocking join.  This local
        # signal is intentionally await-free because ASGI 2.3 may repeatedly
        # cancel the stream task while unwinding its disconnect task group.
        lifecycle.request_stop()


def _streaming_response(
    text: str,
    reference_path: Path | None = None,
    prompt_text: str | None = None,
    cleanup_root: Path | None = None,
) -> StreamingResponse:
    lifecycle = _VoxStreamLifecycle(cleanup_root)
    return _FinalizingStreamingResponse(
        _pcm_stream(
            text,
            reference_path,
            prompt_text,
            lifecycle,
        ),
        finalizer=lifecycle.aclose,
        media_type="audio/pcm",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Audio-Format": "pcm_s16le",
            "X-Audio-Sample-Rate": "48000",
            "X-Audio-Channels": "1",
            "X-Synthetic-Voice": "true",
        },
    )


@app.post("/v1/audio/speech")
async def speech(
    request: SpeechRequest,
    _authenticated: Annotated[None, Depends(_require_worker_token)],
) -> StreamingResponse:
    text = _validate_text(request.input, MAX_TEXT_CHARS, "Input text")
    if request.model != VOXCPM_MODEL:
        raise HTTPException(status_code=400, detail="Requested model is unavailable.")
    if request.voice != "default":
        raise HTTPException(
            status_code=400, detail="Only the default voice is available."
        )
    if request.response_format != "pcm":
        raise HTTPException(status_code=400, detail="Only raw PCM output is available.")
    if not request.stream or request.stream_format != "audio":
        raise HTTPException(
            status_code=400, detail="Streaming audio output is required."
        )
    if request.ref_audio is None:
        if request.ref_text:
            raise HTTPException(
                status_code=400,
                detail="Reference text requires reference audio.",
            )
        return _streaming_response(text)

    normalized_prompt = (
        _validate_text(request.ref_text, MAX_PROMPT_TEXT_CHARS, "Reference text")
        if request.ref_text
        else None
    )
    workdir = Path(tempfile.mkdtemp(prefix="echoweave-voxcpm-"))
    try:
        reference_path = _decode_reference_data_uri(request.ref_audio, workdir)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return _streaming_response(
        text,
        reference_path=reference_path,
        prompt_text=normalized_prompt,
        cleanup_root=workdir,
    )


@app.post("/v1/audio/speech/clone")
async def clone_speech(
    _authenticated: Annotated[None, Depends(_require_worker_token)],
    reference_audio: Annotated[UploadFile, File()],
    input: Annotated[str, Form()],
    reference_authorized: Annotated[bool, Form()],
    prompt_text: Annotated[str | None, Form()] = None,
    model: Annotated[str, Form()] = VOXCPM_MODEL,
    response_format: Annotated[str, Form()] = "pcm",
    stream: Annotated[bool, Form()] = True,
) -> StreamingResponse:
    if not reference_authorized:
        raise HTTPException(
            status_code=403,
            detail="Reference-voice authorization is required.",
        )
    if model != VOXCPM_MODEL:
        raise HTTPException(status_code=400, detail="Requested model is unavailable.")
    if response_format != "pcm" or not stream:
        raise HTTPException(
            status_code=400, detail="Streaming raw PCM output is required."
        )
    text = _validate_text(input, MAX_TEXT_CHARS, "Input text")
    normalized_prompt = (
        _validate_text(prompt_text, MAX_PROMPT_TEXT_CHARS, "Prompt text")
        if prompt_text
        else None
    )

    workdir = Path(tempfile.mkdtemp(prefix="echoweave-voxcpm-"))
    reference_path = workdir / "reference.wav"
    try:
        await _save_reference_audio(reference_audio, reference_path)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return _streaming_response(
        text,
        reference_path=reference_path,
        prompt_text=normalized_prompt,
        cleanup_root=workdir,
    )
