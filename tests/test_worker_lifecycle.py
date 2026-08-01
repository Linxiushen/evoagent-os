from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.requests import ClientDisconnect

import services.soulx_api as soulx
import services.voxcpm_api as voxcpm
from echoweave.auth import ConsentAssertionClaims


def _claims(scope: str, reference_hashes: dict[str, str]) -> ConsentAssertionClaims:
    return ConsentAssertionClaims(
        subject="fictional",
        audience="worker-test",
        issued_at=1,
        expires_at=2,
        jti="worker-test-jti-00000001",
        persona_id="fictional",
        consent_id="test-consent",
        revision=1,
        manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        scopes=frozenset({scope}),
        reference_hashes=reference_hashes,
    )


def _http_scope(spec_version: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/worker-test",
        "raw_path": b"/worker-test",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


async def _disconnect_receive() -> dict:
    return {"type": "http.disconnect"}


async def _fail_response_start(message: dict) -> None:
    if message["type"] == "http.response.start":
        raise OSError("peer disconnected before response headers")


async def test_voxcpm_response_start_failure_removes_reference_audio(
    tmp_path, monkeypatch
):
    real_mkdtemp = tempfile.mkdtemp
    created: list[Path] = []

    def tracked_mkdtemp(*, prefix: str) -> str:
        path = Path(real_mkdtemp(prefix=prefix, dir=tmp_path))
        created.append(path)
        return str(path)

    monkeypatch.setattr(voxcpm.tempfile, "mkdtemp", tracked_mkdtemp)
    wav_header = b"RIFF\x04\x00\x00\x00WAVE"
    request = voxcpm.SpeechRequest(
        input="test",
        ref_audio=(
            "data:audio/wav;base64," + base64.b64encode(wav_header).decode("ascii")
        ),
    )
    response = await voxcpm.speech(
        request,
        _claims(
            "voice_clone",
            {"voice": hashlib.sha256(wav_header).hexdigest()},
        ),
    )

    with pytest.raises(ClientDisconnect):
        await response(
            _http_scope("2.4"),
            _disconnect_receive,
            _fail_response_start,
        )

    assert created
    assert not created[0].exists()


async def test_soulx_response_start_failure_removes_uploaded_biometrics(
    tmp_path, monkeypatch
):
    real_mkdtemp = tempfile.mkdtemp
    created: list[Path] = []

    def tracked_mkdtemp(*, prefix: str) -> str:
        path = Path(real_mkdtemp(prefix=prefix, dir=tmp_path))
        created.append(path)
        return str(path)

    monkeypatch.setattr(soulx.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(soulx, "_supports_process_group_isolation", lambda: True)
    image = UploadFile(io.BytesIO(b"\x89PNG\r\n\x1a\nimage"), filename="avatar.png")
    audio = UploadFile(
        io.BytesIO(b"RIFF\x04\x00\x00\x00WAVE"),
        filename="speech.wav",
    )
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    response = await soulx.stream_avatar(
        _claims(
            "avatar_animation",
            {"image": hashlib.sha256(image_bytes).hexdigest()},
        ),
        image,
        audio,
        "lite",
        "test",
    )

    with pytest.raises(ClientDisconnect):
        await response(
            _http_scope("2.4"),
            _disconnect_receive,
            _fail_response_start,
        )

    assert created
    assert not created[0].exists()


async def test_voxcpm_asgi23_disconnect_waits_for_producer_and_cleans(
    tmp_path, monkeypatch
):
    import numpy as np

    release_producer = threading.Event()
    first_body = asyncio.Event()
    workdir = tmp_path / "voxcpm-private"
    workdir.mkdir()
    reference_path = workdir / "reference.wav"
    reference_path.write_bytes(b"private voice")

    class FakeModel:
        tts_model = type("TTSModel", (), {"sample_rate": 48_000})()

        @staticmethod
        def generate_streaming(**_kwargs):
            yield np.zeros(64, dtype=np.float32)
            release_producer.wait(timeout=5)

    monkeypatch.setattr(voxcpm, "_get_model", lambda: FakeModel())
    response = voxcpm._streaming_response(
        "test",
        reference_path=reference_path,
        cleanup_root=workdir,
    )

    async def receive() -> dict:
        await first_body.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()

    response_task = asyncio.create_task(response(_http_scope("2.3"), receive, send))
    await asyncio.wait_for(first_body.wait(), timeout=2)
    await asyncio.sleep(0.05)
    assert not response_task.done()
    assert workdir.exists()

    release_producer.set()
    await asyncio.wait_for(response_task, timeout=2)

    assert not workdir.exists()
    assert not any(
        thread.name == "echoweave-voxcpm-inference" and thread.is_alive()
        for thread in threading.enumerate()
    )


async def test_voxcpm_stuck_producer_cleanup_is_bounded_and_quarantines(
    tmp_path,
    monkeypatch,
):
    workdir = tmp_path / "voxcpm-stuck-private"
    workdir.mkdir()
    (workdir / "reference.wav").write_bytes(b"private voice")
    release_producer = threading.Event()
    producer_started = threading.Event()
    stop_event = threading.Event()

    def block_forever_without_observing_stop() -> None:
        producer_started.set()
        release_producer.wait()

    producer = threading.Thread(
        target=block_forever_without_observing_stop,
        name="echoweave-voxcpm-test-stuck",
        daemon=True,
    )
    lifecycle = voxcpm._VoxStreamLifecycle(workdir)
    monkeypatch.setattr(voxcpm, "PRODUCER_JOIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(voxcpm, "WORKER_QUARANTINED", False)
    lifecycle.start(producer, stop_event)
    assert await asyncio.to_thread(producer_started.wait, 1)

    try:
        started = time.perf_counter()
        await asyncio.wait_for(lifecycle.aclose(), timeout=0.5)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.3
        assert stop_event.is_set()
        assert producer.is_alive()
        assert voxcpm.WORKER_QUARANTINED is True
        assert workdir.exists()
        assert voxcpm._dependency_status()["runtime_not_quarantined"] is False
        health = await voxcpm.health()
        assert b'"ok":false' in health.body
    finally:
        release_producer.set()
        producer.join(timeout=1)

    deadline = time.monotonic() + 1
    while workdir.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not workdir.exists()


@pytest.mark.parametrize("module", [voxcpm, soulx])
async def test_worker_cleanup_survives_repeated_task_cancellation(module):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = False

    async def finalizer() -> None:
        nonlocal cleanup_finished
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished = True

    task = asyncio.create_task(module._await_cleanup(finalizer))
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished


async def test_soulx_process_reaping_does_not_block_event_loop(tmp_path, monkeypatch):
    workdir = tmp_path / "soulx-private"
    workdir.mkdir()
    (workdir / "avatar.png").write_bytes(b"private image")
    lifecycle = soulx._SoulXStreamLifecycle(workdir)
    lifecycle.process = object()

    def slow_reap(_process, _isolated: bool) -> bool:
        time.sleep(0.2)
        return True

    monkeypatch.setattr(soulx, "_terminate_and_join_soulx_process", slow_reap)
    started = time.perf_counter()
    close_task = asyncio.create_task(lifecycle.aclose())
    await asyncio.sleep(0.02)

    assert time.perf_counter() - started < 0.1
    assert not close_task.done()
    await asyncio.wait_for(close_task, timeout=1)
    assert not workdir.exists()
