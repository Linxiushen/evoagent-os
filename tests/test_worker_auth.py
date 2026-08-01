from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import services.soulx_api as soulx
import services.voxcpm_api as voxcpm
from echoweave.auth import ReplayCache, issue_consent_assertion

VOX_KEY = "voxcpm-worker-key-that-is-at-least-32-bytes"
SOULX_KEY = "soulx-worker-key-that-is-at-least-32-bytes"
MANIFEST_DIGEST = hashlib.sha256(b"manifest").hexdigest()


def _assertion(
    key: str,
    audience: str,
    *,
    scopes: set[str],
    reference_hashes: dict[str, str],
) -> str:
    return issue_consent_assertion(
        key,
        audience=audience,
        persona_id="fictional",
        consent_id="consent-123",
        revision=3,
        manifest_digest=MANIFEST_DIGEST,
        scopes=scopes,
        reference_hashes=reference_hashes,
        ttl_seconds=60,
    )


def test_voxcpm_rejects_raw_shared_token(monkeypatch):
    monkeypatch.setenv("VOXCPM_WORKER_TOKEN", VOX_KEY)
    monkeypatch.delenv("MODEL_WORKER_TOKEN", raising=False)
    voxcpm.ASSERTION_REPLAY_CACHE = ReplayCache()

    response = TestClient(voxcpm.app).post(
        "/v1/audio/speech",
        headers={"X-Worker-Token": VOX_KEY},
        json={"input": "hello"},
    )

    assert response.status_code == 401
    assert VOX_KEY not in response.text


def test_voxcpm_accepts_signed_assertion_but_enforces_scope_and_reference(monkeypatch):
    monkeypatch.setenv("VOXCPM_WORKER_TOKEN", VOX_KEY)
    monkeypatch.delenv("MODEL_WORKER_TOKEN", raising=False)
    voxcpm.ASSERTION_REPLAY_CACHE = ReplayCache()
    client = TestClient(voxcpm.app)
    synthesis = _assertion(
        VOX_KEY,
        "echoweave-voxcpm-worker",
        scopes={"voice_synthesis"},
        reference_hashes={},
    )
    accepted_auth = client.post(
        "/v1/audio/speech",
        headers={"X-Worker-Token": synthesis},
        json={"input": "hello", "model": "unavailable-model"},
    )
    assert accepted_auth.status_code == 400

    wav = b"RIFF\x04\x00\x00\x00WAVE"
    wrong_reference = _assertion(
        VOX_KEY,
        "echoweave-voxcpm-worker",
        scopes={"voice_clone"},
        reference_hashes={"voice": hashlib.sha256(b"wrong").hexdigest()},
    )
    rejected_reference = client.post(
        "/v1/audio/speech",
        headers={"X-Worker-Token": wrong_reference},
        json={
            "input": "hello",
            "ref_audio": (
                "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
            ),
        },
    )
    assert rejected_reference.status_code == 403
    assert "hash" not in rejected_reference.text.lower()


def test_soulx_rejects_raw_token_cross_audience_and_wrong_reference(monkeypatch):
    monkeypatch.setenv("SOULX_WORKER_TOKEN", SOULX_KEY)
    monkeypatch.delenv("MODEL_WORKER_TOKEN", raising=False)
    monkeypatch.setattr(soulx, "_supports_process_group_isolation", lambda: True)
    soulx.ASSERTION_REPLAY_CACHE = ReplayCache()
    client = TestClient(soulx.app)
    files = {
        "image": ("avatar.png", b"\x89PNG\r\n\x1a\nimage", "image/png"),
        "audio": ("speech.wav", b"RIFF\x04\x00\x00\x00WAVE", "audio/wav"),
    }

    raw = client.post(
        "/v1/avatar/stream",
        headers={"X-Worker-Token": SOULX_KEY},
        files=files,
        data={"model_type": "lite", "text": "hello"},
    )
    assert raw.status_code == 401

    cross_audience = _assertion(
        SOULX_KEY,
        "echoweave-voxcpm-worker",
        scopes={"avatar_animation"},
        reference_hashes={"image": hashlib.sha256(files["image"][1]).hexdigest()},
    )
    cross = client.post(
        "/v1/avatar/stream",
        headers={"X-Worker-Token": cross_audience},
        files=files,
        data={"model_type": "lite", "text": "hello"},
    )
    assert cross.status_code == 401

    wrong_reference = _assertion(
        SOULX_KEY,
        "echoweave-soulx-worker",
        scopes={"avatar_animation"},
        reference_hashes={"image": hashlib.sha256(b"wrong").hexdigest()},
    )
    mismatch = client.post(
        "/v1/avatar/stream",
        headers={"X-Worker-Token": wrong_reference},
        files=files,
        data={"model_type": "lite", "text": "hello"},
    )
    assert mismatch.status_code == 403


def _worker_scope(path: str, token: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"x-worker-token", token.encode()),
            (b"content-length", b"1"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


@pytest.mark.parametrize(
    ("module", "key_env", "key", "audience", "path"),
    [
        (
            voxcpm,
            "VOXCPM_WORKER_TOKEN",
            VOX_KEY,
            "echoweave-voxcpm-worker",
            "/v1/audio/speech",
        ),
        (
            soulx,
            "SOULX_WORKER_TOKEN",
            SOULX_KEY,
            "echoweave-soulx-worker",
            "/v1/avatar/stream",
        ),
    ],
)
async def test_worker_middleware_bounds_global_inflight_bodies(
    module,
    key_env,
    key,
    audience,
    path,
    monkeypatch,
):
    monkeypatch.setenv(key_env, key)
    held_token = _assertion(
        key,
        audience,
        scopes={"voice_synthesis", "avatar_animation"},
        reference_hashes={},
    )
    queued_token = _assertion(
        key,
        audience,
        scopes={"voice_synthesis", "avatar_animation"},
        reference_hashes={},
    )
    module.ASSERTION_REPLAY_CACHE = ReplayCache()
    monkeypatch.setattr(module, "WORKER_QUARANTINED", False)
    started = asyncio.Event()
    release = asyncio.Event()

    async def inner(_scope, receive, send):
        assert (await receive())["body"] == b"x"
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = module._BoundedAuthenticatedBody(
        inner,
        {path: 10},
        max_inflight=1,
        body_timeout_seconds=1,
    )

    async def body_receive():
        return {"type": "http.request", "body": b"x", "more_body": False}

    first_messages = []

    async def first_send(message):
        first_messages.append(message)

    first = asyncio.create_task(
        middleware(_worker_scope(path, held_token), body_receive, first_send)
    )
    await asyncio.wait_for(started.wait(), 1)
    rejected_messages = []

    async def rejected_send(message):
        rejected_messages.append(message)

    await middleware(
        _worker_scope(path, queued_token),
        body_receive,
        rejected_send,
    )
    assert rejected_messages[0]["status"] == 429

    release.set()
    await asyncio.wait_for(first, 1)
    assert first_messages[0]["status"] == 204

    admitted_messages = []

    async def admitted_send(message):
        admitted_messages.append(message)

    await middleware(
        _worker_scope(path, queued_token),
        body_receive,
        admitted_send,
    )
    assert admitted_messages[0]["status"] == 204


@pytest.mark.parametrize(
    ("module", "key_env", "key", "audience", "path"),
    [
        (
            voxcpm,
            "VOXCPM_WORKER_TOKEN",
            VOX_KEY,
            "echoweave-voxcpm-worker",
            "/v1/audio/speech",
        ),
        (
            soulx,
            "SOULX_WORKER_TOKEN",
            SOULX_KEY,
            "echoweave-soulx-worker",
            "/v1/avatar/stream",
        ),
    ],
)
async def test_worker_middleware_enforces_total_body_read_deadline(
    module,
    key_env,
    key,
    audience,
    path,
    monkeypatch,
):
    monkeypatch.setenv(key_env, key)
    module.ASSERTION_REPLAY_CACHE = ReplayCache()
    monkeypatch.setattr(module, "WORKER_QUARANTINED", False)
    token = _assertion(
        key,
        audience,
        scopes={"voice_synthesis", "avatar_animation"},
        reference_hashes={},
    )

    async def never_called(_scope, _receive, _send):
        raise AssertionError("timed-out body must not reach the worker")

    async def stalled_receive():
        await asyncio.Event().wait()

    messages = []

    async def send(message):
        messages.append(message)

    middleware = module._BoundedAuthenticatedBody(
        never_called,
        {path: 10},
        max_inflight=1,
        body_timeout_seconds=0.1,
    )
    await middleware(_worker_scope(path, token), stalled_receive, send)

    assert messages[0]["status"] == 408


@pytest.mark.parametrize(
    ("module", "key_env", "key", "audience", "path"),
    [
        (
            voxcpm,
            "VOXCPM_WORKER_TOKEN",
            VOX_KEY,
            "echoweave-voxcpm-worker",
            "/v1/audio/speech",
        ),
        (
            soulx,
            "SOULX_WORKER_TOKEN",
            SOULX_KEY,
            "echoweave-soulx-worker",
            "/v1/avatar/stream",
        ),
    ],
)
async def test_worker_middleware_atomically_consumes_one_time_assertion(
    module,
    key_env,
    key,
    audience,
    path,
    monkeypatch,
):
    monkeypatch.setenv(key_env, key)
    monkeypatch.setattr(module, "WORKER_QUARANTINED", False)
    module.ASSERTION_REPLAY_CACHE = ReplayCache()
    token = _assertion(
        key,
        audience,
        scopes={"voice_synthesis", "avatar_animation"},
        reference_hashes={},
    )
    entered = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def inner(scope, receive, send):
        nonlocal entered
        claims = module._require_worker_token(Request(scope))
        assert claims == scope[module._ASSERTION_SCOPE_KEY]
        assert (await receive())["body"] == b"x"
        entered += 1
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = module._BoundedAuthenticatedBody(
        inner,
        {path: 10},
        max_inflight=2,
        body_timeout_seconds=1,
    )

    async def body_receive():
        return {"type": "http.request", "body": b"x", "more_body": False}

    messages = [[], []]

    async def invoke(index):
        async def send(message):
            messages[index].append(message)

        await middleware(_worker_scope(path, token), body_receive, send)

    requests = [asyncio.create_task(invoke(0)), asyncio.create_task(invoke(1))]
    await asyncio.wait_for(started.wait(), 1)
    done, _ = await asyncio.wait(
        requests,
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert len(done) == 1
    assert next(iter(done)).exception() is None
    assert middleware._inflight == 1
    assert entered == 1

    release.set()
    await asyncio.gather(*requests)
    assert sorted(batch[0]["status"] for batch in messages) == [204, 401]

    replay_messages = []

    async def replay_send(message):
        replay_messages.append(message)

    await middleware(_worker_scope(path, token), body_receive, replay_send)
    assert replay_messages[0]["status"] == 401
    assert entered == 1


@pytest.mark.parametrize("module", [voxcpm, soulx])
def test_worker_dependency_rejects_claims_not_admitted_by_middleware(module):
    request = Request(_worker_scope("/worker-test", "not-a-valid-assertion"))

    with pytest.raises(HTTPException) as raised:
        module._require_worker_token(request)

    assert raised.value.status_code == 401


async def test_voxcpm_quarantined_worker_rejects_before_reading_body(monkeypatch):
    monkeypatch.setenv("VOXCPM_WORKER_TOKEN", VOX_KEY)
    voxcpm.ASSERTION_REPLAY_CACHE = ReplayCache()
    monkeypatch.setattr(voxcpm, "WORKER_QUARANTINED", True)
    token = _assertion(
        VOX_KEY,
        "echoweave-voxcpm-worker",
        scopes={"voice_synthesis"},
        reference_hashes={},
    )
    body_read = False

    async def never_called(_scope, _receive, _send):
        raise AssertionError("quarantined worker must reject before dispatch")

    async def body_receive():
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": b"x", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    middleware = voxcpm._BoundedAuthenticatedBody(
        never_called,
        {"/v1/audio/speech": 10},
        max_inflight=1,
        body_timeout_seconds=1,
    )
    await middleware(
        _worker_scope("/v1/audio/speech", token),
        body_receive,
        send,
    )

    assert messages[0]["status"] == 503
    assert body_read is False
