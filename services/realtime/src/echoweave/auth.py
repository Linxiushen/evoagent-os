from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

MAX_TOKEN_BYTES = 16 * 1024
MAX_TOKEN_LIFETIME_SECONDS = 3_600
MIN_SIGNING_KEY_BYTES = 32
TOKEN_HEADER = {"alg": "HS256", "typ": "EWT", "v": 1}
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
CLAIM_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")
JTI_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SESSION_CLAIMS = frozenset(
    {
        "kind",
        "sub",
        "aud",
        "iat",
        "exp",
        "jti",
        "persona_scope",
        "capabilities",
    }
)
CONSENT_CLAIMS = frozenset(
    {
        "kind",
        "sub",
        "aud",
        "iat",
        "exp",
        "jti",
        "persona_id",
        "consent_id",
        "revision",
        "manifest_digest",
        "scopes",
        "reference_hashes",
    }
)


class TokenValidationError(ValueError):
    """A deliberately non-sensitive token validation failure."""


@dataclass(frozen=True, slots=True)
class SessionTokenClaims:
    subject: str
    audience: str
    issued_at: int
    expires_at: int
    jti: str
    persona_scope: frozenset[str]
    capabilities: frozenset[str]


@dataclass(frozen=True, slots=True)
class ConsentAssertionClaims:
    subject: str
    audience: str
    issued_at: int
    expires_at: int
    jti: str
    persona_id: str
    consent_id: str
    revision: int
    manifest_digest: str
    scopes: frozenset[str]
    reference_hashes: dict[str, str]


class ReplayCache:
    """Thread-safe, bounded one-time-token cache keyed only by opaque JTIs."""

    def __init__(self, max_entries: int = 65_536) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError("max_entries must be an integer")
        if not 1 <= max_entries <= 1_000_000:
            raise ValueError("max_entries is out of range")
        self.max_entries = max_entries
        self._entries: dict[str, int] = {}
        self._lock = RLock()

    def consume(self, jti: str, expires_at: int, *, now: int | None = None) -> None:
        current = _unix_time(now)
        with self._lock:
            if self._entries:
                expired = [
                    key for key, expiry in self._entries.items() if expiry < current
                ]
                for key in expired:
                    self._entries.pop(key, None)
            if jti in self._entries:
                raise TokenValidationError("token has already been used")
            if len(self._entries) >= self.max_entries:
                raise TokenValidationError("token replay cache is at capacity")
            self._entries[jti] = expires_at


def _unix_time(value: float | None = None) -> int:
    raw = time.time() if value is None else value
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError("time must be numeric")
    if not math.isfinite(raw) or raw < 0:
        raise ValueError("time is invalid")
    return int(raw)


def _signing_key(value: str) -> bytes:
    if type(value) is not str or len(value.encode("utf-8")) < MIN_SIGNING_KEY_BYTES:
        raise ValueError("signing key must be at least 32 bytes")
    return value.encode("utf-8")


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        return base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise TokenValidationError("token encoding is invalid") from exc


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TokenValidationError("token contains duplicate claims")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                TokenValidationError("token contains a non-finite number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TokenValidationError("token payload is invalid") from exc
    if type(decoded) is not dict:
        raise TokenValidationError("token payload is invalid")
    return decoded


def _encode_token(payload: dict[str, Any], signing_key: str) -> str:
    key = _signing_key(signing_key)
    encoded_header = _b64encode(_canonical_json(TOKEN_HEADER))
    encoded_payload = _b64encode(_canonical_json(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64encode(signature)}"


def _decode_token(
    token: str,
    signing_key: str,
    *,
    expected_audience: str,
    expected_kind: str,
    expected_claims: frozenset[str],
    max_lifetime_seconds: int,
    clock_skew_seconds: int,
    now: float | None,
) -> dict[str, Any]:
    key = _signing_key(signing_key)
    if (
        type(token) is not str
        or not token
        or len(token.encode("utf-8")) > MAX_TOKEN_BYTES
    ):
        raise TokenValidationError("token is invalid")
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise TokenValidationError("token is invalid")
    encoded_header, encoded_payload, encoded_signature = token.split(".")
    supplied_signature = _b64decode(encoded_signature)
    if len(supplied_signature) != hashlib.sha256().digest_size:
        raise TokenValidationError("token signature is invalid")
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise TokenValidationError("token signature is invalid")
    header = _strict_json_object(_b64decode(encoded_header))
    if header != TOKEN_HEADER:
        raise TokenValidationError("token header is invalid")
    payload = _strict_json_object(_b64decode(encoded_payload))
    if payload.keys() != expected_claims or payload.get("kind") != expected_kind:
        raise TokenValidationError("token claims are invalid")
    if payload.get("aud") != expected_audience:
        raise TokenValidationError("token audience is invalid")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if type(issued_at) is not int or type(expires_at) is not int:
        raise TokenValidationError("token timestamps are invalid")
    if (
        isinstance(max_lifetime_seconds, bool)
        or not isinstance(max_lifetime_seconds, int)
        or not 1 <= max_lifetime_seconds <= MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise ValueError("max_lifetime_seconds is out of range")
    if (
        isinstance(clock_skew_seconds, bool)
        or not isinstance(clock_skew_seconds, int)
        or not 0 <= clock_skew_seconds <= 60
    ):
        raise ValueError("clock_skew_seconds is out of range")
    current = _unix_time(now)
    if issued_at > current + clock_skew_seconds:
        raise TokenValidationError("token is not yet valid")
    if expires_at < current - clock_skew_seconds:
        raise TokenValidationError("token has expired")
    lifetime = expires_at - issued_at
    if lifetime < 1 or lifetime > max_lifetime_seconds:
        raise TokenValidationError("token lifetime is invalid")
    return payload


def _claim_string(value: Any, name: str) -> str:
    if type(value) is not str or CLAIM_TOKEN_PATTERN.fullmatch(value) is None:
        raise TokenValidationError(f"{name} is invalid")
    return value


def _jti(value: Any) -> str:
    if type(value) is not str or JTI_PATTERN.fullmatch(value) is None:
        raise TokenValidationError("jti is invalid")
    return value


def _claim_set(value: Any, name: str, *, max_items: int = 128) -> frozenset[str]:
    if type(value) is not list or not 1 <= len(value) <= max_items:
        raise TokenValidationError(f"{name} is invalid")
    parsed = [_claim_string(item, name) for item in value]
    if len(parsed) != len(set(parsed)) or parsed != sorted(parsed):
        raise TokenValidationError(f"{name} must be unique and sorted")
    return frozenset(parsed)


def _issue_times(
    ttl_seconds: int,
    *,
    now: float | None,
) -> tuple[int, int]:
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise ValueError("ttl_seconds is out of range")
    issued_at = _unix_time(now)
    return issued_at, issued_at + ttl_seconds


def issue_session_token(
    signing_key: str,
    *,
    subject: str,
    audience: str,
    persona_scope: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    capabilities: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    ttl_seconds: int = 300,
    jti: str | None = None,
    now: float | None = None,
) -> str:
    issued_at, expires_at = _issue_times(ttl_seconds, now=now)
    payload = {
        "kind": "session",
        "sub": _claim_string(subject, "subject"),
        "aud": _claim_string(audience, "audience"),
        "iat": issued_at,
        "exp": expires_at,
        "jti": _jti(jti or secrets.token_urlsafe(24)),
        "persona_scope": sorted(
            _claim_set(sorted(persona_scope), "persona_scope", max_items=64)
        ),
        "capabilities": sorted(
            _claim_set(sorted(capabilities), "capabilities", max_items=128)
        ),
    }
    return _encode_token(payload, signing_key)


def verify_session_token(
    token: str,
    signing_key: str,
    *,
    audience: str,
    max_lifetime_seconds: int = 300,
    clock_skew_seconds: int = 5,
    replay_cache: ReplayCache | None = None,
    consume: bool = True,
    now: float | None = None,
) -> SessionTokenClaims:
    payload = _decode_token(
        token,
        signing_key,
        expected_audience=audience,
        expected_kind="session",
        expected_claims=SESSION_CLAIMS,
        max_lifetime_seconds=max_lifetime_seconds,
        clock_skew_seconds=clock_skew_seconds,
        now=now,
    )
    claims = SessionTokenClaims(
        subject=_claim_string(payload["sub"], "subject"),
        audience=_claim_string(payload["aud"], "audience"),
        issued_at=payload["iat"],
        expires_at=payload["exp"],
        jti=_jti(payload["jti"]),
        persona_scope=_claim_set(
            payload["persona_scope"], "persona_scope", max_items=64
        ),
        capabilities=_claim_set(payload["capabilities"], "capabilities", max_items=128),
    )
    if consume and replay_cache is not None:
        current = _unix_time(now)
        replay_cache.consume(
            claims.jti,
            max(claims.expires_at, current + clock_skew_seconds),
            now=current,
        )
    return claims


def authorize_session_claims(
    claims: SessionTokenClaims,
    *,
    persona_id: str,
    capabilities: set[str] | frozenset[str] | tuple[str, ...],
) -> None:
    if persona_id not in claims.persona_scope:
        raise TokenValidationError("token does not authorize this persona")
    if not set(capabilities).issubset(claims.capabilities):
        raise TokenValidationError("token does not authorize requested capabilities")


def issue_consent_assertion(
    signing_key: str,
    *,
    audience: str,
    persona_id: str,
    consent_id: str,
    revision: int,
    manifest_digest: str,
    scopes: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    reference_hashes: dict[str, str],
    ttl_seconds: int = 120,
    jti: str | None = None,
    now: float | None = None,
) -> str:
    issued_at, expires_at = _issue_times(ttl_seconds, now=now)
    if type(revision) is not int or revision < 0:
        raise ValueError("revision is invalid")
    if (
        type(manifest_digest) is not str
        or SHA256_PATTERN.fullmatch(manifest_digest) is None
    ):
        raise ValueError("manifest_digest is invalid")
    normalized_hashes: dict[str, str] = {}
    if type(reference_hashes) is not dict or len(reference_hashes) > 2:
        raise ValueError("reference_hashes is invalid")
    for kind, digest in sorted(reference_hashes.items()):
        if kind not in {"image", "voice"}:
            raise ValueError("reference hash kind is invalid")
        if type(digest) is not str or SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("reference hash is invalid")
        normalized_hashes[kind] = digest
    normalized_persona = _claim_string(persona_id, "persona_id")
    payload = {
        "kind": "consent",
        "sub": normalized_persona,
        "aud": _claim_string(audience, "audience"),
        "iat": issued_at,
        "exp": expires_at,
        "jti": _jti(jti or secrets.token_urlsafe(24)),
        "persona_id": normalized_persona,
        "consent_id": _claim_string(consent_id, "consent_id"),
        "revision": revision,
        "manifest_digest": manifest_digest,
        "scopes": sorted(_claim_set(sorted(scopes), "scopes", max_items=32)),
        "reference_hashes": normalized_hashes,
    }
    return _encode_token(payload, signing_key)


def verify_consent_assertion(
    token: str,
    signing_key: str,
    *,
    audience: str,
    max_lifetime_seconds: int = 300,
    clock_skew_seconds: int = 5,
    replay_cache: ReplayCache | None = None,
    consume: bool = True,
    now: float | None = None,
) -> ConsentAssertionClaims:
    payload = _decode_token(
        token,
        signing_key,
        expected_audience=audience,
        expected_kind="consent",
        expected_claims=CONSENT_CLAIMS,
        max_lifetime_seconds=max_lifetime_seconds,
        clock_skew_seconds=clock_skew_seconds,
        now=now,
    )
    persona_id = _claim_string(payload["persona_id"], "persona_id")
    subject = _claim_string(payload["sub"], "subject")
    if not hmac.compare_digest(subject, persona_id):
        raise TokenValidationError("consent subject is invalid")
    revision = payload["revision"]
    if type(revision) is not int or revision < 0:
        raise TokenValidationError("consent revision is invalid")
    digest = payload["manifest_digest"]
    if type(digest) is not str or SHA256_PATTERN.fullmatch(digest) is None:
        raise TokenValidationError("manifest digest is invalid")
    raw_hashes = payload["reference_hashes"]
    if type(raw_hashes) is not dict or len(raw_hashes) > 2:
        raise TokenValidationError("reference hashes are invalid")
    hashes: dict[str, str] = {}
    for kind, reference_digest in raw_hashes.items():
        if kind not in {"image", "voice"}:
            raise TokenValidationError("reference hash kind is invalid")
        if (
            type(reference_digest) is not str
            or SHA256_PATTERN.fullmatch(reference_digest) is None
        ):
            raise TokenValidationError("reference hash is invalid")
        hashes[kind] = reference_digest
    claims = ConsentAssertionClaims(
        subject=subject,
        audience=_claim_string(payload["aud"], "audience"),
        issued_at=payload["iat"],
        expires_at=payload["exp"],
        jti=_jti(payload["jti"]),
        persona_id=persona_id,
        consent_id=_claim_string(payload["consent_id"], "consent_id"),
        revision=revision,
        manifest_digest=digest,
        scopes=_claim_set(payload["scopes"], "scopes", max_items=32),
        reference_hashes=hashes,
    )
    if consume and replay_cache is not None:
        current = _unix_time(now)
        replay_cache.consume(
            claims.jti,
            max(claims.expires_at, current + clock_skew_seconds),
            now=current,
        )
    return claims


def authorize_consent_claims(
    claims: ConsentAssertionClaims,
    *,
    required_scope: str,
    reference_hashes: dict[str, str] | None = None,
) -> None:
    if required_scope not in claims.scopes:
        raise TokenValidationError("consent assertion scope is insufficient")
    expected = reference_hashes or {}
    if claims.reference_hashes.keys() != expected.keys():
        raise TokenValidationError("consent assertion reference set does not match")
    for kind, digest in expected.items():
        claimed = claims.reference_hashes.get(kind, "")
        if not hmac.compare_digest(claimed, digest):
            raise TokenValidationError(
                "consent assertion reference hash does not match"
            )
