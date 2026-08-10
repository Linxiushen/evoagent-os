"""Hardened HTTP bridge for SoulX-FlashHead's official streaming worker.

Run this service in the dedicated SoulX Python/CUDA environment. It accepts
only authenticated requests, bounds all media, confines generated paths to a
per-request directory, and burns a permanent synthetic-media watermark into
every returned MP4 segment.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import logging
import math
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from echoweave.auth import (
    ConsentAssertionClaims,
    ReplayCache,
    TokenValidationError,
    authorize_consent_claims,
    verify_consent_assertion,
)

LOGGER = logging.getLogger("echoweave.soulx_worker")

SOULX_REPO_DIR = Path(os.environ.get("SOULX_REPO_DIR", "/opt/SoulX-FlashHead"))
SOULX_CKPT_DIR = os.environ.get(
    "SOULX_CKPT_DIR",
    "/models/SoulX-FlashHead-1_3B",
)
SOULX_WAV2VEC_DIR = os.environ.get(
    "SOULX_WAV2VEC_DIR",
    "/models/wav2vec2-base-960h",
)
MAX_IMAGE_BYTES = int(os.environ.get("SOULX_MAX_IMAGE_BYTES", "10485760"))
MAX_AUDIO_BYTES = int(os.environ.get("SOULX_MAX_AUDIO_BYTES", "67108864"))
MAX_SEGMENT_BYTES = int(os.environ.get("SOULX_MAX_SEGMENT_BYTES", "33554432"))
MAX_TEXT_CHARS = int(os.environ.get("SOULX_MAX_TEXT_CHARS", "2000"))
FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("SOULX_FFMPEG_TIMEOUT_SECONDS", "90"))
INFERENCE_TIMEOUT_SECONDS = int(
    os.environ.get("SOULX_INFERENCE_TIMEOUT_SECONDS", "1800")
)
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + MAX_AUDIO_BYTES + 2 * 1024 * 1024
MAX_INFLIGHT_REQUESTS = int(os.environ.get("SOULX_MAX_INFLIGHT_REQUESTS", "1"))
REQUEST_BODY_TIMEOUT_SECONDS = float(
    os.environ.get("SOULX_REQUEST_BODY_TIMEOUT_SECONDS", "45")
)

INFERENCE_LOCK = asyncio.Lock()
WORKER_QUARANTINED = False
ASSERTION_REPLAY_CACHE = ReplayCache()
_ASSERTION_SCOPE_KEY = "echoweave.worker_assertion_claims"

app = FastAPI(
    title="EchoWeave SoulX Worker",
    version="0.2.0",
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
    """Own request resources across the complete ASGI response invocation."""

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


def _configured_worker_token() -> str:
    token = os.environ.get("SOULX_WORKER_TOKEN", "").strip()
    if not token:
        token = os.environ.get("MODEL_WORKER_TOKEN", "").strip()
    return token if len(token.encode("utf-8")) >= 32 else ""


def _configured_worker_audience() -> str:
    return os.environ.get(
        "SOULX_WORKER_AUDIENCE",
        "echoweave-soulx-worker",
    ).strip()


def _configured_assertion_max_ttl() -> int:
    try:
        value = int(os.environ.get("ECHOWEAVE_WORKER_ASSERTION_MAX_TTL_SECONDS", "300"))
    except ValueError:
        return 0
    return value if 1 <= value <= 300 else 0


def _verify_worker_assertion(
    supplied: str,
    *,
    consume: bool,
) -> ConsentAssertionClaims:
    expected = _configured_worker_token()
    max_ttl = _configured_assertion_max_ttl()
    audience = _configured_worker_audience()
    if not expected or not audience or not max_ttl:
        raise HTTPException(
            status_code=503,
            detail="Worker authentication is unavailable.",
        )
    try:
        return verify_consent_assertion(
            supplied,
            expected,
            audience=audience,
            max_lifetime_seconds=max_ttl,
            replay_cache=ASSERTION_REPLAY_CACHE,
            consume=consume,
        )
    except (TokenValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail="Worker authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _authorize_worker_request(
    claims: ConsentAssertionClaims | None,
    *,
    reference_hashes: dict[str, str],
) -> None:
    if not isinstance(claims, ConsentAssertionClaims):
        raise HTTPException(
            status_code=401,
            detail="Worker authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        authorize_consent_claims(
            claims,
            required_scope="avatar_animation",
            reference_hashes=reference_hashes,
        )
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=403,
            detail="Consent assertion rejected.",
        ) from exc


def _require_worker_token(request: Request) -> ConsentAssertionClaims:
    claims = request.scope.get(_ASSERTION_SCOPE_KEY)
    supplied = _scope_worker_token(request.scope)
    if not isinstance(claims, ConsentAssertionClaims) or not supplied:
        raise HTTPException(
            status_code=401,
            detail="Worker authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    verified = _verify_worker_assertion(supplied, consume=False)
    if verified != claims:
        raise HTTPException(
            status_code=401,
            detail="Worker authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


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
    """Authenticate, globally admit, then read one bounded body under a deadline."""

    def __init__(
        self,
        app: ASGIApp,
        route_limits: dict[str, int],
        max_inflight: int,
        body_timeout_seconds: float,
    ) -> None:
        self.app = app
        self.route_limits = route_limits
        self.max_inflight = max(1, min(max_inflight, 64))
        self.body_timeout_seconds = (
            max(0.1, min(body_timeout_seconds, 300.0))
            if math.isfinite(body_timeout_seconds)
            else 45.0
        )
        self._inflight = 0
        self._admission_lock = asyncio.Lock()

    async def _admit(self, supplied: str) -> ConsentAssertionClaims | None:
        async with self._admission_lock:
            if WORKER_QUARANTINED:
                raise HTTPException(
                    status_code=503,
                    detail="Worker is quarantined after an unsafe process shutdown.",
                )
            if self._inflight >= self.max_inflight:
                return None
            claims = _verify_worker_assertion(supplied, consume=True)
            self._inflight += 1
            return claims

    async def _release(self) -> None:
        async with self._admission_lock:
            self._inflight = max(0, self._inflight - 1)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.route_limits:
            await self.app(scope, receive, send)
            return

        supplied = _scope_worker_token(scope)
        try:
            if not supplied:
                raise HTTPException(status_code=401)
            _verify_worker_assertion(supplied, consume=False)
        except HTTPException as exc:
            await _guard_response(
                scope,
                receive,
                send,
                exc.status_code,
                (
                    "Worker authentication is unavailable."
                    if exc.status_code == 503
                    else "Worker authentication failed."
                ),
            )
            return

        try:
            claims = await self._admit(supplied)
        except HTTPException as exc:
            await _guard_response(
                scope,
                receive,
                send,
                exc.status_code,
                (
                    "Worker authentication failed."
                    if exc.status_code == 401
                    else "Worker is unavailable."
                ),
            )
            return
        if claims is None:
            await _guard_response(
                scope,
                receive,
                send,
                429,
                "Worker request capacity is exhausted.",
            )
            return
        try:
            async with asyncio.timeout(self.body_timeout_seconds):
                replay_receive = await self._read_body(scope, receive, send)
            if replay_receive is not None:
                admitted_scope = dict(scope)
                admitted_scope[_ASSERTION_SCOPE_KEY] = claims
                await self.app(admitted_scope, replay_receive, send)
        except TimeoutError:
            await _guard_response(
                scope,
                receive,
                send,
                408,
                "Request body deadline exceeded.",
            )
        finally:
            await self._release()

    async def _read_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> Receive | None:

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

        return replay_receive


app.add_middleware(
    _BoundedAuthenticatedBody,
    route_limits={"/v1/avatar/stream": MAX_REQUEST_BYTES},
    max_inflight=MAX_INFLIGHT_REQUESTS,
    body_timeout_seconds=REQUEST_BODY_TIMEOUT_SECONDS,
)


def _load_official_streamer():
    if not SOULX_REPO_DIR.is_dir():
        raise RuntimeError("SoulX repository is unavailable.")
    repo = str(SOULX_REPO_DIR.resolve())
    if repo not in sys.path:
        sys.path.insert(0, repo)
    module = importlib.import_module("gradio_app_streaming")
    return module.run_inference_streaming


def _supports_process_group_isolation() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "setsid")
        and hasattr(os, "killpg")
        and hasattr(signal, "SIGKILL")
    )


@lru_cache(maxsize=1)
def _ffmpeg_supports_drawtext() -> bool:
    executable = shutil.which("ffmpeg")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-filters"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "drawtext" in result.stdout


def _dependency_status() -> dict[str, bool]:
    return {
        "official_worker": (SOULX_REPO_DIR / "gradio_app_streaming.py").is_file(),
        "checkpoint": Path(SOULX_CKPT_DIR).is_dir(),
        "wav2vec": Path(SOULX_WAV2VEC_DIR).is_dir(),
        "ffmpeg_drawtext": _ffmpeg_supports_drawtext(),
        "authentication": bool(_configured_worker_token()),
        "process_group_isolation": _supports_process_group_isolation(),
        "runtime_not_quarantined": not WORKER_QUARANTINED,
    }


@app.get("/health")
async def health() -> JSONResponse:
    checks = _dependency_status()
    return JSONResponse(
        {
            "ok": all(checks.values()),
            "checks": checks,
            "model_type": "lite",
            "synthetic_media": True,
        },
        headers={"Cache-Control": "no-store"},
    )


async def _save_upload_limited(
    upload: UploadFile,
    destination: Path,
    byte_limit: int,
) -> None:
    size = 0
    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > byte_limit:
                    raise HTTPException(
                        status_code=413,
                        detail="Uploaded media is too large.",
                    )
                output.write(chunk)
    finally:
        await upload.close()
    if size == 0:
        raise HTTPException(status_code=400, detail="Uploaded media is empty.")


def _validated_image_path(staged_path: Path) -> Path:
    header = staged_path.read_bytes()[:16]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif header.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise HTTPException(
            status_code=415,
            detail="Avatar image must be PNG, JPEG, or WebP.",
        )
    validated = staged_path.with_suffix(suffix)
    staged_path.replace(validated)
    return validated


def _validate_wav(path: Path) -> None:
    header = path.read_bytes()[:12]
    if len(header) < 12 or not (header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
        raise HTTPException(
            status_code=415,
            detail="Audio input must be a RIFF/WAVE file.",
        )


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _validated_output_path(raw_path: str | os.PathLike[str], root: Path) -> Path:
    candidate = Path(raw_path)
    if candidate.suffix.lower() != ".mp4":
        raise RuntimeError("SoulX returned an unsupported output type.")
    try:
        absolute_root = Path(os.path.abspath(root))
        absolute_candidate = Path(os.path.abspath(candidate))
        unresolved_relative = absolute_candidate.relative_to(absolute_root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeError("SoulX returned an output outside its sandbox.") from exc
    cursor = absolute_root
    if cursor.is_symlink():
        raise RuntimeError("SoulX returned a symbolic-link output.")
    for part in unresolved_relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError("SoulX returned a symbolic-link output.")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeError("SoulX returned an output outside its sandbox.") from exc
    if not resolved.is_file():
        raise RuntimeError("SoulX returned an invalid output.")
    if resolved.stat().st_size > MAX_SEGMENT_BYTES:
        raise RuntimeError("SoulX output exceeded the configured size limit.")
    return resolved


def _burn_watermark(source: Path, destination: Path) -> None:
    executable = shutil.which("ffmpeg")
    if not executable or not _ffmpeg_supports_drawtext():
        raise RuntimeError("A drawtext-capable ffmpeg installation is required.")
    filter_expression = (
        "drawtext=text='AI DIGITAL TWIN':"
        "x=w-tw-24:y=h-th-24:"
        "fontcolor=white:fontsize=h/28:"
        "box=1:boxcolor=black@0.65:boxborderw=10"
    )
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        filter_expression,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Could not watermark the SoulX output.") from exc
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Could not watermark the SoulX output.")
    if (
        destination.is_symlink()
        or not destination.is_file()
        or destination.stat().st_size > MAX_SEGMENT_BYTES
    ):
        destination.unlink(missing_ok=True)
        raise RuntimeError("Watermarked output failed validation.")


def _read_file_limited(path: Path, byte_limit: int) -> bytes:
    with path.open("rb") as stream:
        payload = stream.read(byte_limit + 1)
    if len(payload) > byte_limit:
        raise RuntimeError("Generated output exceeded the configured size limit.")
    return payload


def _noop_progress(*_args, **_kwargs) -> None:
    return None


def _soulx_isolated_child(
    send_connection,
    start_event,
    workdir: str,
    image_path: str,
    audio_path: str,
    output_root: str,
    watermarked_root: str,
) -> None:
    """Run the unmodified official generator and its threads in one process group."""
    try:
        if not _supports_process_group_isolation():
            send_connection.send(("isolation_error", None))
            return
        os.setsid()
        send_connection.send(("ready", None))
        if not start_event.wait(timeout=30):
            return
        streamer = _load_official_streamer()
        with _working_directory(Path(workdir)):
            stream = streamer(
                SOULX_CKPT_DIR,
                SOULX_WAV2VEC_DIR,
                "lite",
                image_path,
                audio_path,
                9999,
                True,
                progress=_noop_progress,
            )
            for index, segment_path in enumerate(stream):
                validated = _validated_output_path(segment_path, Path(output_root))
                watermarked = Path(watermarked_root) / f"segment-{index:06d}.mp4"
                _burn_watermark(validated, watermarked)
                send_connection.send(("segment", str(watermarked)))
        send_connection.send(("done", None))
    except BaseException:
        LOGGER.exception("Isolated SoulX generation failed.")
        try:
            send_connection.send(("error", None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send_connection.close()


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_and_join_soulx_process(process, isolated: bool) -> bool:
    """Stop every descendant before reporting that request cleanup is safe."""
    if process is None or process.pid is None:
        return True
    process_group_id = process.pid
    try:
        if process.is_alive():
            if isolated:
                try:
                    os.killpg(process_group_id, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
        process.join(timeout=5)

        group_alive = isolated and _process_group_exists(process_group_id)
        if process.is_alive() or group_alive:
            if isolated:
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.join(timeout=5)

        deadline = time.monotonic() + 5
        while isolated and _process_group_exists(process_group_id):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        stopped = not process.is_alive() and not (
            isolated and _process_group_exists(process_group_id)
        )
        if stopped:
            process.close()
        return stopped
    except (AssertionError, OSError, ValueError):
        LOGGER.exception("Could not safely reap the isolated SoulX process.")
        return False


class _SoulXStreamLifecycle:
    """Serialize process reaping, lock release and private-directory cleanup."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.process = None
        self.isolated = False
        self.lock_acquired = False
        self._close_task: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_impl())
        await asyncio.shield(self._close_task)

    async def _close_impl(self) -> None:
        global WORKER_QUARANTINED

        stopped = await asyncio.to_thread(
            _terminate_and_join_soulx_process,
            self.process,
            self.isolated,
        )
        if stopped:
            shutil.rmtree(self.workdir, ignore_errors=True)
            if self.lock_acquired:
                INFERENCE_LOCK.release()
                self.lock_acquired = False
            return

        WORKER_QUARANTINED = True
        # Deliberately retain both the lock and request directory. Releasing
        # either could race an unkillable process that still owns the GPU.


async def _next_child_message(connection, process, deadline: float):
    while True:
        try:
            if connection.poll(0):
                return connection.recv()
        except (EOFError, OSError) as exc:
            raise RuntimeError("The isolated SoulX process disconnected.") from exc
        if not process.is_alive():
            try:
                if connection.poll(0):
                    return connection.recv()
            except (EOFError, OSError):
                pass
            raise RuntimeError("The isolated SoulX process exited unexpectedly.")
        if time.monotonic() >= deadline:
            raise RuntimeError("The isolated SoulX process timed out.")
        await asyncio.sleep(0.05)


@app.post("/v1/avatar/stream")
async def stream_avatar(
    assertion: Annotated[
        ConsentAssertionClaims,
        Depends(_require_worker_token),
    ],
    image: Annotated[UploadFile, File()],
    audio: Annotated[UploadFile, File()],
    model_type: Annotated[str, Form()] = "lite",
    text: Annotated[str, Form()] = "",
) -> StreamingResponse:
    if not _supports_process_group_isolation():
        raise HTTPException(
            status_code=503,
            detail=(
                "SoulX inference requires POSIX process-group isolation; "
                "this bridge fails closed on unsupported platforms."
            ),
        )
    if WORKER_QUARANTINED:
        raise HTTPException(
            status_code=503,
            detail="SoulX worker is quarantined after an unsafe process shutdown.",
        )
    if model_type != "lite":
        raise HTTPException(
            status_code=400,
            detail="The realtime worker only permits SoulX Lite.",
        )
    if len(text) > MAX_TEXT_CHARS or "\x00" in text:
        raise HTTPException(status_code=400, detail="Text metadata is invalid.")

    workdir = Path(tempfile.mkdtemp(prefix="echoweave-soulx-"))
    staged_image = workdir / "avatar.upload"
    audio_path = workdir / "speech.wav"
    try:
        await _save_upload_limited(image, staged_image, MAX_IMAGE_BYTES)
        image_path = _validated_image_path(staged_image)
        _authorize_worker_request(
            assertion,
            reference_hashes={
                "image": hashlib.sha256(image_path.read_bytes()).hexdigest()
            },
        )
        await _save_upload_limited(audio, audio_path, MAX_AUDIO_BYTES)
        _validate_wav(audio_path)
        output_root = workdir / "gradio_results"
        output_root.mkdir(mode=0o700)
        watermarked_root = workdir / "watermarked"
        watermarked_root.mkdir(mode=0o700)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    lifecycle = _SoulXStreamLifecycle(workdir)

    async def generate():
        receive_connection = None
        send_connection = None
        try:
            await INFERENCE_LOCK.acquire()
            lifecycle.lock_acquired = True
            if WORKER_QUARANTINED:
                raise RuntimeError("SoulX worker is quarantined.")

            context = multiprocessing.get_context("spawn")
            receive_connection, send_connection = context.Pipe(duplex=False)
            start_event = context.Event()
            lifecycle.process = context.Process(
                target=_soulx_isolated_child,
                args=(
                    send_connection,
                    start_event,
                    str(workdir),
                    str(image_path),
                    str(audio_path),
                    str(output_root),
                    str(watermarked_root),
                ),
                daemon=True,
            )
            lifecycle.process.start()
            send_connection.close()
            send_connection = None

            deadline = time.monotonic() + INFERENCE_TIMEOUT_SECONDS
            while True:
                message_type, value = await _next_child_message(
                    receive_connection,
                    lifecycle.process,
                    deadline,
                )
                if message_type == "ready":
                    lifecycle.isolated = True
                    start_event.set()
                    continue
                if message_type == "segment":
                    validated = _validated_output_path(value, watermarked_root)
                    segment = _read_file_limited(validated, MAX_SEGMENT_BYTES)
                    index = int(validated.stem.rsplit("-", 1)[-1])
                    payload = {
                        "index": index,
                        "mime_type": "video/mp4",
                        "duration_ms": 3000,
                        "data_b64": base64.b64encode(segment).decode("ascii"),
                        "synthetic": True,
                        "watermark": "AI DIGITAL TWIN",
                        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                    yield json.dumps(payload, ensure_ascii=False) + "\n"
                    validated.unlink(missing_ok=True)
                    continue
                if message_type == "done":
                    break
                if message_type == "isolation_error":
                    raise RuntimeError(
                        "SoulX process-group isolation could not be established."
                    )
                raise RuntimeError("The isolated SoulX process failed.")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("SoulX generation failed.")
            raise
        finally:
            if send_connection is not None:
                try:
                    send_connection.close()
                except OSError:
                    pass
            if receive_connection is not None:
                try:
                    receive_connection.close()
                except OSError:
                    pass
            # Reaping may take several seconds for a stubborn CUDA/ffmpeg child.
            # The lifecycle runs it off the ASGI loop, and the response-level
            # finalizer awaits the same idempotent task after disconnect scopes end.
            await lifecycle.aclose()

    return _FinalizingStreamingResponse(
        generate(),
        finalizer=lifecycle.aclose,
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Synthetic-Media": "true",
            "X-AI-Watermark": "AI DIGITAL TWIN",
        },
    )
