from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from echoweave.auth import (
    ReplayCache,
    TokenValidationError,
    authorize_consent_claims,
    authorize_session_claims,
    issue_consent_assertion,
    issue_session_token,
    verify_consent_assertion,
    verify_session_token,
)
from echoweave.contracts import AudioFrame, AvatarSegment
from echoweave.runtime import ConsentBoundAvatar, ConsentBoundTTS

SIGNING_KEY = "session-signing-key-that-is-at-least-32-bytes"
WORKER_KEY = "worker-signing-key-that-is-at-least-32-bytes"


def _session_token(**overrides) -> str:
    values = {
        "subject": "user-123",
        "audience": "echoweave-rtc",
        "persona_scope": {"demo", "fictional"},
        "capabilities": {"input.text", "output.text_stream"},
        "ttl_seconds": 60,
        "jti": "session-jti-000000000001",
        "now": 1_000,
    }
    values.update(overrides)
    return issue_session_token(SIGNING_KEY, **values)


def _consent_assertion(reference_hashes=None, **overrides) -> str:
    values = {
        "audience": "echoweave-voxcpm-worker",
        "persona_id": "fictional",
        "consent_id": "consent-123",
        "revision": 7,
        "manifest_digest": hashlib.sha256(b"manifest").hexdigest(),
        "scopes": {"voice_clone", "voice_synthesis"},
        "reference_hashes": reference_hashes or {},
        "ttl_seconds": 60,
        "jti": "consent-jti-000000000001",
        "now": 1_000,
    }
    values.update(overrides)
    return issue_consent_assertion(WORKER_KEY, **values)


def test_session_token_enforces_persona_capabilities_and_one_time_use():
    replay = ReplayCache(8)
    token = _session_token()
    claims = verify_session_token(
        token,
        SIGNING_KEY,
        audience="echoweave-rtc",
        max_lifetime_seconds=60,
        replay_cache=replay,
        now=1_010,
    )
    authorize_session_claims(
        claims,
        persona_id="fictional",
        capabilities={"input.text", "output.text_stream"},
    )

    with pytest.raises(TokenValidationError, match="already been used"):
        verify_session_token(
            token,
            SIGNING_KEY,
            audience="echoweave-rtc",
            max_lifetime_seconds=60,
            replay_cache=replay,
            now=1_011,
        )
    with pytest.raises(TokenValidationError, match="persona"):
        authorize_session_claims(claims, persona_id="other", capabilities=set())
    with pytest.raises(TokenValidationError, match="capabilities"):
        authorize_session_claims(
            claims,
            persona_id="demo",
            capabilities={"input.audio_pcm16"},
        )


@pytest.mark.parametrize(
    ("key", "audience", "now"),
    [
        ("different-signing-key-that-is-also-long-enough", "echoweave-rtc", 1_010),
        (SIGNING_KEY, "wrong-audience", 1_010),
        (SIGNING_KEY, "echoweave-rtc", 1_100),
    ],
)
def test_session_token_rejects_wrong_signature_audience_and_expiry(key, audience, now):
    with pytest.raises(TokenValidationError):
        verify_session_token(
            _session_token(),
            key,
            audience=audience,
            max_lifetime_seconds=60,
            now=now,
        )


def test_session_token_rejects_payload_tampering_without_leaking_key():
    token = _session_token()
    header, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["persona_scope"] = ["admin"]
    tampered_payload = (
        base64.urlsafe_b64encode(json.dumps(decoded, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )

    with pytest.raises(TokenValidationError) as caught:
        verify_session_token(
            f"{header}.{tampered_payload}.{signature}",
            SIGNING_KEY,
            audience="echoweave-rtc",
            now=1_010,
        )
    assert SIGNING_KEY not in str(caught.value)


def test_consent_assertion_binds_scope_reference_and_replay():
    voice_digest = hashlib.sha256(b"voice").hexdigest()
    token = _consent_assertion({"voice": voice_digest})
    replay = ReplayCache(8)
    claims = verify_consent_assertion(
        token,
        WORKER_KEY,
        audience="echoweave-voxcpm-worker",
        max_lifetime_seconds=60,
        replay_cache=replay,
        now=1_010,
    )
    authorize_consent_claims(
        claims,
        required_scope="voice_clone",
        reference_hashes={"voice": voice_digest},
    )

    with pytest.raises(TokenValidationError, match="hash"):
        authorize_consent_claims(
            claims,
            required_scope="voice_clone",
            reference_hashes={"voice": hashlib.sha256(b"other").hexdigest()},
        )
    with pytest.raises(TokenValidationError, match="scope"):
        authorize_consent_claims(
            claims,
            required_scope="avatar_animation",
            reference_hashes={"voice": voice_digest},
        )
    with pytest.raises(TokenValidationError, match="already been used"):
        verify_consent_assertion(
            token,
            WORKER_KEY,
            audience="echoweave-voxcpm-worker",
            max_lifetime_seconds=60,
            replay_cache=replay,
            now=1_011,
        )


def test_consent_assertion_rejects_cross_worker_audience():
    with pytest.raises(TokenValidationError, match="audience"):
        verify_consent_assertion(
            _consent_assertion(),
            WORKER_KEY,
            audience="echoweave-soulx-worker",
            max_lifetime_seconds=60,
            now=1_010,
        )


async def test_runtime_wrappers_issue_fresh_reference_bound_worker_assertions():
    voice = b"consented voice"
    image = b"consented image"
    manifest_digest = hashlib.sha256(b"manifest").hexdigest()
    persona = SimpleNamespace(
        persona_id="fictional",
        consent_id="consent-123",
        manifest_revision=4,
        manifest_digest=manifest_digest,
        reference_voice_data=voice,
        reference_image_data=image,
    )

    class TTS:
        worker_token = ""

        async def synthesize(self, _text, _persona, _cancel_event):
            claims = verify_consent_assertion(
                self.worker_token,
                WORKER_KEY,
                audience="echoweave-voxcpm-worker",
                max_lifetime_seconds=120,
            )
            authorize_consent_claims(
                claims,
                required_scope="voice_clone",
                reference_hashes={"voice": hashlib.sha256(voice).hexdigest()},
            )
            yield AudioFrame(b"\x00\x00", 48_000)

        async def aclose(self):
            return None

    class Avatar:
        worker_token = ""

        async def animate(self, *_args):
            claims = verify_consent_assertion(
                self.worker_token,
                WORKER_KEY,
                audience="echoweave-soulx-worker",
                max_lifetime_seconds=120,
            )
            authorize_consent_claims(
                claims,
                required_scope="avatar_animation",
                reference_hashes={"image": hashlib.sha256(image).hexdigest()},
            )
            yield AvatarSegment(kind="test")

        async def aclose(self):
            return None

    raw_tts = TTS()
    tts = ConsentBoundTTS(
        raw_tts,
        signing_key=WORKER_KEY,
        audience="echoweave-voxcpm-worker",
        ttl_seconds=120,
    )
    frames = [frame async for frame in tts.synthesize("hi", persona, object())]
    assert frames
    assert raw_tts.worker_token == ""

    raw_avatar = Avatar()
    avatar = ConsentBoundAvatar(
        raw_avatar,
        signing_key=WORKER_KEY,
        audience="echoweave-soulx-worker",
        ttl_seconds=120,
    )
    segments = [
        segment
        async for segment in avatar.animate(
            "hi",
            b"\x00\x00",
            48_000,
            persona,
            object(),
        )
    ]
    assert segments
    assert raw_avatar.worker_token == ""
