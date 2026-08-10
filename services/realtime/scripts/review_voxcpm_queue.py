"""Run a loopback-only browser UI for reviewing an immutable VoxCPM queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import threading
import urllib.parse
import wave
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
QUEUE_KIND = "echoweave-voxcpm2-manual-review-queue"
DECISIONS_KIND = "echoweave-voxcpm2-review-decisions"
BURNED_SUBTITLE_KIND = "echoweave-burned-subtitle-review-evidence"
AUDIO_ONLY_KIND = "echoweave-audio-only-review-evidence"
COOKIE_NAME_PREFIX = "echoweave_review_"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DECISIONS_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024
MAX_CORRECTED_TEXT_CHARS = 8_000
MAX_CLIPS = 2_000
MAX_AUDIO_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_RANGE_DIGITS = 20
CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
QUEUE_FALSE_FLAGS = (
    "training_ready",
    "ready_for_local_training",
    "runtime_promotion_allowed",
    "human_review",
    "approved",
    "approved_for_training",
    "transcript_verified",
)
DECISION_INPUT_FIELDS = {
    "clip_id",
    "corrected_text",
    "target_speaker_only",
    "transcript_verified",
    "no_third_party_speech",
    "no_background_music",
    "approved",
}
DECISION_STORED_FIELDS = DECISION_INPUT_FIELDS | {"gates_satisfied", "updated_at"}


class ReviewToolError(ValueError):
    """Raised when a queue, request, or decisions file is unsafe."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _reject_link_components(path: Path, label: str) -> None:
    absolute = _absolute(path)
    for component in reversed((absolute, *absolute.parents)):
        if _is_link(component):
            raise ReviewToolError(f"{label} must not contain symbolic links")


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_file(path: Path, label: str) -> Path:
    candidate = _absolute(path)
    _reject_link_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReviewToolError(f"{label} could not be resolved") from exc
    if not resolved.is_file():
        raise ReviewToolError(f"{label} must be a regular file")
    return resolved


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewToolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewToolError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReviewToolError(f"{label} must contain a JSON object")
    return value


def _read_json(
    path: Path, label: str, maximum_bytes: int
) -> tuple[dict[str, Any], str, int]:
    resolved = _safe_file(path, label)
    size = resolved.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise ReviewToolError(f"{label} has an unsafe size")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ReviewToolError(f"{label} could not be read") from exc
    if len(raw) != size:
        raise ReviewToolError(f"{label} changed while it was being read")
    return _decode_json(raw, label), hashlib.sha256(raw).hexdigest(), size


def _strict_number(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise ReviewToolError(f"{label} must be a finite JSON number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ReviewToolError(f"{label} must be a finite JSON number") from exc
    if not math.isfinite(number):
        raise ReviewToolError(f"{label} must be a finite JSON number")
    return number


def _strict_false(value: Any, label: str) -> None:
    if value is not False:
        raise ReviewToolError(f"{label} must be false in an immutable review queue")


def _strict_text(value: Any, label: str, maximum: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise ReviewToolError(f"{label} is invalid")
    cleaned = value.strip()
    if not allow_empty and not cleaned:
        raise ReviewToolError(f"{label} must be non-empty")
    return cleaned


def _review_evidence_status(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("review_evidence")
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "ocr_performed",
        "reason_code",
    }:
        raise ReviewToolError("review queue evidence status is invalid")
    kind = value.get("kind")
    performed = value.get("ocr_performed")
    reason = value.get("reason_code")
    if kind == BURNED_SUBTITLE_KIND:
        if performed is not True or reason is not None:
            raise ReviewToolError("burned-subtitle evidence status is invalid")
    elif kind == AUDIO_ONLY_KIND:
        if performed is not False or reason != "audio_only_no_video_stream":
            raise ReviewToolError("audio-only evidence status is invalid")
    else:
        raise ReviewToolError("review queue evidence kind is unsupported")
    return {"kind": kind, "ocr_performed": performed, "reason_code": reason}


@dataclass(frozen=True)
class ReviewClip:
    clip_id: str
    audio_path: Path
    audio_sha256: str
    audio_size_bytes: int
    duration_seconds: float
    candidate_transcript: str
    subtitle_text: str
    similarity: float
    caption_count: int

    def public_payload(self, decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "duration_seconds": self.duration_seconds,
            "candidate_transcript": self.candidate_transcript,
            "ocr_evidence": {
                "subtitle_text": self.subtitle_text,
                "similarity": self.similarity,
                "caption_count": self.caption_count,
            },
            "audio_url": "/api/audio/" + urllib.parse.quote(self.clip_id, safe=""),
            "decision": decision,
        }


@dataclass(frozen=True)
class QueueSnapshot:
    manifest_path: Path
    root: Path
    sha256: str
    size_bytes: int
    review_evidence: dict[str, Any]
    clips: tuple[ReviewClip, ...]
    clips_by_id: dict[str, ReviewClip]

    @classmethod
    def load(cls, manifest_path: Path) -> QueueSnapshot:
        manifest_path = _safe_file(manifest_path, "review queue manifest")
        payload, digest, size = _read_json(
            manifest_path, "review queue manifest", MAX_MANIFEST_BYTES
        )
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != SCHEMA_VERSION
        ):
            raise ReviewToolError(
                f"review queue schema_version must be {SCHEMA_VERSION}"
            )
        if payload.get("kind") != QUEUE_KIND:
            raise ReviewToolError("manifest is not a VoxCPM manual review queue")
        if payload.get("review_only") is not True:
            raise ReviewToolError("review queue must be marked review_only")
        for field in QUEUE_FALSE_FLAGS:
            _strict_false(payload.get(field), f"review queue.{field}")
        review_evidence = _review_evidence_status(payload)
        raw_clips = payload.get("clips")
        if (
            not isinstance(raw_clips, list)
            or not raw_clips
            or len(raw_clips) > MAX_CLIPS
        ):
            raise ReviewToolError("review queue clips array is invalid")

        root = manifest_path.parent.resolve(strict=True)
        clips: list[ReviewClip] = []
        seen_ids: set[str] = set()
        seen_names: dict[str, str] = {}
        seen_paths: set[Path] = set()
        for index, raw_clip in enumerate(raw_clips):
            if not isinstance(raw_clip, dict):
                raise ReviewToolError(f"clips[{index}] must be an object")
            clip_id = raw_clip.get("clip_id")
            if not isinstance(clip_id, str) or not CLIP_ID.fullmatch(clip_id):
                raise ReviewToolError(f"clips[{index}].clip_id is invalid")
            if clip_id in seen_ids:
                raise ReviewToolError(f"duplicate clip_id: {clip_id}")
            seen_ids.add(clip_id)
            name_key = f"{clip_id}.wav".casefold()
            if name_key in seen_names:
                raise ReviewToolError(
                    "case-insensitive clip filename collision: "
                    f"{seen_names[name_key]} and {clip_id}"
                )
            seen_names[name_key] = clip_id
            for field in (
                "human_review",
                "approved",
                "approved_for_training",
                "transcript_verified",
            ):
                _strict_false(raw_clip.get(field), f"clips[{index}].{field}")

            raw_relative = raw_clip.get("audio_path")
            if (
                not isinstance(raw_relative, str)
                or not raw_relative
                or "\\" in raw_relative
                or "\x00" in raw_relative
            ):
                raise ReviewToolError(f"clips[{index}].audio_path is invalid")
            relative = PurePosixPath(raw_relative)
            if (
                relative.is_absolute()
                or relative.as_posix() != raw_relative
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ReviewToolError(f"clips[{index}].audio_path is not contained")
            audio_candidate = _absolute(root.joinpath(*relative.parts))
            _reject_link_components(audio_candidate, f"clips[{index}].audio_path")
            try:
                audio_path = audio_candidate.resolve(strict=True)
                audio_path.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ReviewToolError(
                    f"clips[{index}].audio_path is not contained"
                ) from exc
            if not audio_path.is_file() or audio_path.suffix.lower() != ".wav":
                raise ReviewToolError(f"clips[{index}].audio_path must be a WAV file")
            if audio_path in seen_paths:
                raise ReviewToolError(f"duplicate audio path: {raw_relative}")
            seen_paths.add(audio_path)

            expected_digest = raw_clip.get("audio_sha256")
            if not isinstance(expected_digest, str) or not SHA256.fullmatch(
                expected_digest
            ):
                raise ReviewToolError(f"clips[{index}].audio_sha256 is invalid")
            expected_size = raw_clip.get("audio_size_bytes")
            if (
                type(expected_size) is not int
                or expected_size <= 0
                or expected_size > MAX_AUDIO_BYTES
            ):
                raise ReviewToolError(f"clips[{index}].audio_size_bytes is invalid")
            actual_digest, actual_size = _sha256(audio_path)
            if actual_digest != expected_digest or actual_size != expected_size:
                raise ReviewToolError(f"clips[{index}] audio binding does not match")

            duration = _strict_number(
                raw_clip.get("duration_seconds"), f"clips[{index}].duration_seconds"
            )
            if not 3.0 <= duration <= 30.0:
                raise ReviewToolError(f"clips[{index}].duration_seconds is invalid")
            try:
                with wave.open(str(audio_path), "rb") as stream:
                    channels = stream.getnchannels()
                    sample_width = stream.getsampwidth()
                    sample_rate = stream.getframerate()
                    frames = stream.getnframes()
                    compression = stream.getcomptype()
            except (OSError, EOFError, wave.Error) as exc:
                raise ReviewToolError(f"clips[{index}] WAV is invalid") from exc
            actual_duration = frames / sample_rate if sample_rate else 0.0
            if (
                (channels, sample_width, sample_rate, compression)
                != (1, 2, 16_000, "NONE")
                or frames <= 0
                or abs(actual_duration - duration) > 0.02
            ):
                raise ReviewToolError(
                    f"clips[{index}] WAV format or duration is invalid"
                )

            transcript = _strict_text(
                raw_clip.get("candidate_transcript"),
                f"clips[{index}].candidate_transcript",
                MAX_CORRECTED_TEXT_CHARS,
                allow_empty=False,
            )
            ocr = raw_clip.get("ocr_evidence")
            if not isinstance(ocr, dict):
                raise ReviewToolError(f"clips[{index}].ocr_evidence must be an object")
            subtitle = _strict_text(
                ocr.get("subtitle_text"),
                f"clips[{index}].ocr_evidence.subtitle_text",
                MAX_CORRECTED_TEXT_CHARS,
                allow_empty=True,
            )
            similarity = _strict_number(
                ocr.get("similarity"),
                f"clips[{index}].ocr_evidence.similarity",
            )
            if not 0 <= similarity <= 1:
                raise ReviewToolError(f"clips[{index}] OCR similarity is invalid")
            caption_count = ocr.get("caption_count")
            if type(caption_count) is not int or caption_count < 0:
                raise ReviewToolError(f"clips[{index}] OCR caption_count is invalid")
            clips.append(
                ReviewClip(
                    clip_id=clip_id,
                    audio_path=audio_path,
                    audio_sha256=actual_digest,
                    audio_size_bytes=actual_size,
                    duration_seconds=round(actual_duration, 6),
                    candidate_transcript=transcript,
                    subtitle_text=subtitle,
                    similarity=similarity,
                    caption_count=caption_count,
                )
            )
        return cls(
            manifest_path=manifest_path,
            root=root,
            sha256=digest,
            size_bytes=size,
            review_evidence=review_evidence,
            clips=tuple(clips),
            clips_by_id={clip.clip_id: clip for clip in clips},
        )

    def verify_manifest(self) -> None:
        _reject_link_components(self.manifest_path, "review queue manifest")
        try:
            digest, size = _sha256(self.manifest_path)
        except OSError as exc:
            raise ReviewToolError(
                "review queue manifest is no longer readable"
            ) from exc
        if digest != self.sha256 or size != self.size_bytes:
            raise ReviewToolError("review queue manifest changed after server startup")

    def verify_audio(self, clip: ReviewClip) -> None:
        _reject_link_components(clip.audio_path, f"audio for {clip.clip_id}")
        try:
            digest, size = _sha256(clip.audio_path)
        except OSError as exc:
            raise ReviewToolError(f"audio changed for {clip.clip_id}") from exc
        if digest != clip.audio_sha256 or size != clip.audio_size_bytes:
            raise ReviewToolError(f"audio changed for {clip.clip_id}")

    def public_payload(self, decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        satisfied = sum(
            1 for decision in decisions.values() if decision["gates_satisfied"] is True
        )
        return {
            "queue_sha256": self.sha256,
            "review_evidence": self.review_evidence,
            "total_clips": len(self.clips),
            "review_gates_satisfied": satisfied,
            "all_review_gates_satisfied": satisfied == len(self.clips),
            "clips": [
                clip.public_payload(
                    decisions.get(clip.clip_id, _empty_decision(clip.clip_id))
                )
                for clip in self.clips
            ],
        }


def _empty_decision(clip_id: str) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "corrected_text": "",
        "target_speaker_only": False,
        "transcript_verified": False,
        "no_third_party_speech": False,
        "no_background_music": False,
        "approved": False,
        "gates_satisfied": False,
        "updated_at": "",
    }


def _validate_decision(
    raw: Any,
    known_clip_ids: set[str],
    *,
    stored: bool,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReviewToolError("decision must be a JSON object")
    expected = DECISION_STORED_FIELDS if stored else DECISION_INPUT_FIELDS
    if set(raw) != expected:
        missing = expected - raw.keys()
        unknown = raw.keys() - expected
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise ReviewToolError("decision fields are invalid: " + "; ".join(detail))
    clip_id = raw.get("clip_id")
    if not isinstance(clip_id, str) or clip_id not in known_clip_ids:
        raise ReviewToolError("decision clip_id is not in the queue")
    corrected_text = _strict_text(
        raw.get("corrected_text"),
        "decision.corrected_text",
        MAX_CORRECTED_TEXT_CHARS,
        allow_empty=True,
    )
    boolean_fields = (
        "target_speaker_only",
        "transcript_verified",
        "no_third_party_speech",
        "no_background_music",
        "approved",
    )
    values: dict[str, bool] = {}
    for field in boolean_fields:
        if type(raw.get(field)) is not bool:
            raise ReviewToolError(f"decision.{field} must be a JSON boolean")
        values[field] = raw[field]
    prerequisites = (
        bool(corrected_text)
        and values["target_speaker_only"]
        and values["transcript_verified"]
        and values["no_third_party_speech"]
        and values["no_background_music"]
    )
    if values["transcript_verified"] and not corrected_text:
        raise ReviewToolError("verified transcript requires corrected_text")
    if values["approved"] and not prerequisites:
        raise ReviewToolError("approval requires every review prerequisite")
    gates_satisfied = prerequisites and values["approved"]
    if stored:
        if raw.get("gates_satisfied") is not gates_satisfied:
            raise ReviewToolError(
                "stored gates_satisfied does not match decision fields"
            )
        updated_at = raw.get("updated_at")
        if not isinstance(updated_at, str) or len(updated_at) > 64:
            raise ReviewToolError("stored updated_at is invalid")
    else:
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "clip_id": clip_id,
        "corrected_text": corrected_text,
        **values,
        "gates_satisfied": gates_satisfied,
        "updated_at": updated_at,
    }


class DecisionStore:
    def __init__(self, path: Path, queue: QueueSnapshot) -> None:
        self.path = _absolute(path)
        self.queue = queue
        self._lock = threading.RLock()
        if self.path.suffix.lower() != ".json":
            raise ReviewToolError("decisions path must end in .json")
        if self.path.name.casefold() == "train.jsonl":
            raise ReviewToolError("decisions path must not be a training manifest")
        _reject_link_components(self.path, "decisions path")
        try:
            self.path.relative_to(queue.root)
        except ValueError:
            pass
        else:
            raise ReviewToolError("decisions file must be outside the immutable queue")
        if self.path.exists() and not self.path.is_file():
            raise ReviewToolError("decisions path must be a file")
        self.load()

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return {}
            payload, _, _ = _read_json(self.path, "decisions file", MAX_DECISIONS_BYTES)
            if (
                type(payload.get("schema_version")) is not int
                or payload["schema_version"] != SCHEMA_VERSION
                or payload.get("kind") != DECISIONS_KIND
            ):
                raise ReviewToolError("decisions file schema is invalid")
            for field in (
                "training_ready",
                "ready_for_local_training",
                "runtime_promotion_allowed",
            ):
                if payload.get(field) is not False:
                    raise ReviewToolError(f"decisions file {field} must be false")
            queue_binding = payload.get("queue")
            if not isinstance(queue_binding, dict) or set(queue_binding) != {
                "path",
                "sha256",
                "size_bytes",
            }:
                raise ReviewToolError("decisions queue binding is invalid")
            if (
                queue_binding.get("sha256") != self.queue.sha256
                or queue_binding.get("size_bytes") != self.queue.size_bytes
            ):
                raise ReviewToolError("decisions file is bound to another queue")
            raw_decisions = payload.get("decisions")
            if not isinstance(raw_decisions, list) or len(raw_decisions) > len(
                self.queue.clips
            ):
                raise ReviewToolError("decisions array is invalid")
            decisions: dict[str, dict[str, Any]] = {}
            known = set(self.queue.clips_by_id)
            for raw in raw_decisions:
                decision = _validate_decision(raw, known, stored=True)
                if decision["clip_id"] in decisions:
                    raise ReviewToolError("decisions file has duplicate clip_id")
                decisions[decision["clip_id"]] = decision
            return decisions

    def record(self, raw: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        with self._lock:
            self.queue.verify_manifest()
            decisions = self.load()
            decision = _validate_decision(
                raw, set(self.queue.clips_by_id), stored=False
            )
            decisions[decision["clip_id"]] = decision
            self._write(decisions)
            return decision, decisions

    def _write(self, decisions: dict[str, dict[str, Any]]) -> None:
        ordered = [
            decisions[clip.clip_id]
            for clip in self.queue.clips
            if clip.clip_id in decisions
        ]
        satisfied = sum(
            1 for decision in ordered if decision["gates_satisfied"] is True
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": DECISIONS_KIND,
            "queue": {
                "path": str(self.queue.manifest_path),
                "sha256": self.queue.sha256,
                "size_bytes": self.queue.size_bytes,
            },
            "training_ready": False,
            "ready_for_local_training": False,
            "runtime_promotion_allowed": False,
            "decisions": ordered,
            "statistics": {
                "total_clips": len(self.queue.clips),
                "decisions_recorded": len(ordered),
                "review_gates_satisfied": satisfied,
                "all_review_gates_satisfied": satisfied == len(self.queue.clips),
            },
        }
        data = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )
        if len(data) > MAX_DECISIONS_BYTES:
            raise ReviewToolError("decisions file would exceed its size limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _reject_link_components(self.path.parent, "decisions parent")
        if _is_link(self.path):
            raise ReviewToolError("decisions path must not be a symbolic link")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}-", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if _is_link(self.path):
                raise ReviewToolError("decisions path became a symbolic link")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        queue: QueueSnapshot,
        decisions: DecisionStore,
        *,
        access_token: str,
        csrf_token: str,
    ) -> None:
        self.queue = queue
        self.decisions = decisions
        self.access_token = access_token
        self.csrf_token = csrf_token
        self.cookie_name = (
            f"{COOKIE_NAME_PREFIX}{queue.sha256[:8]}_{secrets.token_hex(8)}"
        )
        super().__init__(address, ReviewRequestHandler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def launch_url(self) -> str:
        token = urllib.parse.quote(self.access_token, safe="")
        return f"{self.origin}/login?token={token}"


class ReviewRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EchoWeaveReview"
    sys_version = ""
    server: ReviewHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _security_headers(self, *, nonce: str | None = None) -> dict[str, str]:
        script_source = f"'nonce-{nonce}'" if nonce else "'none'"
        return {
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"script-src {script_source}; "
                "style-src 'unsafe-inline'; media-src 'self'; connect-src 'self'; "
                "img-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
        nonce: str | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        all_headers = self._security_headers(nonce=nonce)
        if headers:
            all_headers.update(headers)
        if self.close_connection:
            all_headers["Connection"] = "close"
        all_headers["Content-Type"] = content_type
        all_headers["Content-Length"] = str(len(body))
        for key, value in all_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_problem(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _valid_host(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("Host", ""), f"127.0.0.1:{self.server.server_port}"
        )

    def _authenticated(self) -> bool:
        raw_cookie = self.headers.get("Cookie", "")
        try:
            cookie = SimpleCookie(raw_cookie)
            morsel = cookie.get(self.server.cookie_name)
        except CookieError:
            return False
        return bool(
            morsel and secrets.compare_digest(morsel.value, self.server.access_token)
        )

    def _require_session(self) -> bool:
        if not self._valid_host():
            self._send_problem(HTTPStatus.BAD_REQUEST, "invalid Host header")
            return False
        if not self._authenticated():
            self._send_problem(HTTPStatus.UNAUTHORIZED, "authentication required")
            return False
        return True

    def do_GET(self) -> None:
        self._handle_get(head_only=False)

    def do_HEAD(self) -> None:
        self._handle_get(head_only=True)

    def _handle_get(self, *, head_only: bool) -> None:
        if len(self.path) > 2048:
            self._send_problem(HTTPStatus.REQUEST_URI_TOO_LONG, "request URI too long")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/login":
            self._login(parsed, head_only=head_only)
            return
        if not self._require_session():
            return
        try:
            if parsed.path == "/":
                nonce = secrets.token_urlsafe(18)
                body = _render_ui(self.server.csrf_token, nonce)
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    "text/html; charset=utf-8",
                    nonce=nonce,
                    head_only=head_only,
                )
                return
            if parsed.path == "/api/queue":
                self.server.queue.verify_manifest()
                payload = self.server.queue.public_payload(self.server.decisions.load())
                body = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    "application/json; charset=utf-8",
                    head_only=head_only,
                )
                return
            prefix = "/api/audio/"
            if parsed.path.startswith(prefix) and parsed.query == "":
                encoded_id = parsed.path[len(prefix) :]
                clip_id = urllib.parse.unquote(encoded_id)
                if not clip_id or "/" in clip_id or "\\" in clip_id:
                    self._send_problem(HTTPStatus.NOT_FOUND, "audio clip not found")
                    return
                clip = self.server.queue.clips_by_id.get(clip_id)
                if clip is None:
                    self._send_problem(HTTPStatus.NOT_FOUND, "audio clip not found")
                    return
                self._send_audio(clip, head_only=head_only)
                return
            self._send_problem(HTTPStatus.NOT_FOUND, "resource not found")
        except ReviewToolError as exc:
            self._send_problem(HTTPStatus.CONFLICT, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return

    def _login(self, parsed: urllib.parse.SplitResult, *, head_only: bool) -> None:
        if not self._valid_host():
            self._send_problem(HTTPStatus.BAD_REQUEST, "invalid Host header")
            return
        try:
            query = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True, max_num_fields=4
            )
        except ValueError:
            self._send_problem(HTTPStatus.BAD_REQUEST, "invalid login request")
            return
        values = query.get("token", [])
        if len(values) != 1 or not secrets.compare_digest(
            values[0], self.server.access_token
        ):
            self._send_problem(HTTPStatus.UNAUTHORIZED, "invalid access token")
            return
        self._send_bytes(
            HTTPStatus.SEE_OTHER,
            b"",
            "text/plain; charset=utf-8",
            headers={
                "Location": "/",
                "Set-Cookie": (
                    f"{self.server.cookie_name}={self.server.access_token}; "
                    "Path=/; HttpOnly; SameSite=Strict"
                ),
            },
            head_only=head_only,
        )

    def _send_audio(self, clip: ReviewClip, *, head_only: bool) -> None:
        self.server.queue.verify_manifest()
        self.server.queue.verify_audio(clip)
        size = clip.audio_size_bytes
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        raw_range = self.headers.get("Range")
        if raw_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range.strip())
            if (
                match is None
                or (not match.group(1) and not match.group(2))
                or len(match.group(1)) > MAX_RANGE_DIGITS
                or len(match.group(2)) > MAX_RANGE_DIGITS
            ):
                self._send_bytes(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    b"",
                    "audio/wav",
                    headers={"Content-Range": f"bytes */{size}"},
                    head_only=head_only,
                )
                return
            if not match.group(1):
                suffix = int(match.group(2))
                if suffix <= 0:
                    start = size
                else:
                    start = max(0, size - suffix)
            else:
                start = int(match.group(1))
            if match.group(2) and match.group(1):
                end = min(int(match.group(2)), size - 1)
            if start >= size or start > end:
                self._send_bytes(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    b"",
                    "audio/wav",
                    headers={"Content-Range": f"bytes */{size}"},
                    head_only=head_only,
                )
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        headers = {"Accept-Ranges": "bytes"}
        if status == HTTPStatus.PARTIAL_CONTENT:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self.send_response(status)
        response_headers = self._security_headers()
        response_headers.update(headers)
        response_headers["Content-Type"] = "audio/wav"
        response_headers["Content-Length"] = str(length)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if head_only:
            return
        with clip.audio_path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ReviewToolError(f"audio changed for {clip.clip_id}")
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        # Rejected requests may still have unread bodies. Closing every POST
        # prevents those bytes from being parsed as the next keep-alive request.
        self.close_connection = True
        if len(self.path) > 2048:
            self._send_problem(HTTPStatus.REQUEST_URI_TOO_LONG, "request URI too long")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/decisions" or parsed.query:
            self._send_problem(HTTPStatus.NOT_FOUND, "resource not found")
            return
        if not self._require_session():
            return
        if self.headers.get("Transfer-Encoding"):
            self._send_problem(
                HTTPStatus.BAD_REQUEST, "chunked requests are not accepted"
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._send_problem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )
            return
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            self._send_problem(
                HTTPStatus.LENGTH_REQUIRED, "valid Content-Length required"
            )
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_problem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large"
            )
            return
        try:
            raw = self.rfile.read(length)
        except OSError:
            self._send_problem(HTTPStatus.BAD_REQUEST, "request body could not be read")
            return
        if len(raw) != length:
            self._send_problem(HTTPStatus.BAD_REQUEST, "incomplete request body")
            return
        csrf = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(csrf, self.server.csrf_token):
            self._send_problem(HTTPStatus.FORBIDDEN, "invalid CSRF token")
            return
        origin = self.headers.get("Origin")
        if origin and not secrets.compare_digest(origin, self.server.origin):
            self._send_problem(HTTPStatus.FORBIDDEN, "invalid Origin header")
            return
        try:
            request = _decode_json(raw, "decision request")
            decision, decisions = self.server.decisions.record(request)
            state = self.server.queue.public_payload(decisions)
            self._send_json(
                HTTPStatus.OK,
                {
                    "decision": decision,
                    "review_gates_satisfied": state["review_gates_satisfied"],
                    "all_review_gates_satisfied": state["all_review_gates_satisfied"],
                },
            )
        except ReviewToolError as exc:
            self._send_problem(HTTPStatus.BAD_REQUEST, str(exc))
        except OSError:
            self._send_problem(
                HTTPStatus.INTERNAL_SERVER_ERROR, "decision write failed"
            )

    def do_OPTIONS(self) -> None:
        self._send_problem(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")


def _render_ui(csrf_token: str, nonce: str) -> bytes:
    csrf_json = json.dumps(csrf_token).replace("<", "\\u003c")
    template = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VoxCPM Review</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #18201d; background: #f3f5f4; }
    * { box-sizing: border-box; }
    body { margin: 0; min-width: 320px; }
    button, textarea, input { font: inherit; }
    button { border: 1px solid #bcc5c1; background: #fff; color: #17201c; height: 36px; padding: 0 13px; border-radius: 5px; cursor: pointer; }
    button:hover { border-color: #76847d; background: #f7f9f8; }
    button:focus-visible, textarea:focus-visible, input:focus-visible { outline: 3px solid #9bc5b2; outline-offset: 1px; }
    .topbar { height: 64px; padding: 0 22px; display: flex; align-items: center; justify-content: space-between; gap: 16px; background: #fff; border-bottom: 1px solid #d7ddda; }
    h1 { margin: 0; font-size: 18px; font-weight: 680; letter-spacing: 0; }
    .digest { display: block; margin-top: 3px; font: 11px ui-monospace, monospace; color: #66716c; }
    .overall { text-align: right; }
    .overall strong { display: block; font-size: 13px; }
    .overall span { font-size: 12px; color: #68736e; }
    .layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); height: calc(100vh - 64px); }
    .sidebar { background: #eef1ef; border-right: 1px solid #d2d9d5; overflow: auto; }
    .sidebar-head { position: sticky; top: 0; z-index: 1; padding: 14px 16px; background: #eef1ef; border-bottom: 1px solid #d2d9d5; font-size: 12px; color: #59645f; }
    .clip-list { list-style: none; margin: 0; padding: 7px; }
    .clip-list button { width: 100%; height: 48px; border-color: transparent; background: transparent; padding: 0 10px; display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; align-items: center; gap: 8px; text-align: left; }
    .clip-list button:hover { background: #e4e9e6; }
    .clip-list button.active { background: #fff; border-color: #c8d0cc; }
    .clip-index { color: #78827d; font-size: 11px; }
    .clip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 12px ui-monospace, monospace; }
    .clip-state { width: 8px; height: 8px; border-radius: 50%; background: #a5ada9; }
    .clip-state.pass { background: #16805a; }
    .workspace { overflow: auto; padding: 22px clamp(18px, 4vw, 54px) 38px; }
    .workspace-inner { max-width: 1020px; margin: 0 auto; }
    .clip-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
    .clip-heading h2 { margin: 0; font-size: 17px; letter-spacing: 0; }
    .clip-heading span { font-size: 12px; color: #68736e; }
    .nav { display: flex; gap: 7px; }
    .audio-band { background: #202824; padding: 14px 16px; border-radius: 6px; }
    audio { display: block; width: 100%; height: 38px; }
    .metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid #d4dad7; border-top: 0; background: #fff; }
    .metric { padding: 11px 14px; border-right: 1px solid #d4dad7; }
    .metric:last-child { border-right: 0; }
    .metric span { display: block; color: #68736e; font-size: 11px; }
    .metric strong { font-size: 14px; }
    .similarity-track { height: 4px; margin-top: 7px; background: #dde2df; overflow: hidden; }
    .similarity-fill { height: 100%; background: #2c8061; width: 0; }
    .compare { display: grid; grid-template-columns: 1fr 1fr; margin-top: 24px; border-top: 1px solid #ccd4d0; border-bottom: 1px solid #ccd4d0; }
    .text-source { padding: 17px 18px 20px 0; min-width: 0; }
    .text-source + .text-source { padding-left: 18px; border-left: 1px solid #ccd4d0; }
    h3 { margin: 0 0 9px; font-size: 12px; text-transform: uppercase; color: #5c6862; letter-spacing: 0; }
    .source-text { margin: 0; font-size: 15px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
    .edit-section { padding-top: 22px; }
    label.title { display: block; margin-bottom: 8px; font-size: 13px; font-weight: 650; }
    textarea { display: block; width: 100%; min-height: 128px; resize: vertical; padding: 12px 13px; border: 1px solid #bfc8c3; border-radius: 5px; background: #fff; line-height: 1.55; }
    fieldset { margin: 18px 0 0; padding: 0; border: 0; }
    legend { padding: 0; margin-bottom: 9px; font-size: 13px; font-weight: 650; }
    .gates { display: grid; grid-template-columns: 1fr 1fr; border: 1px solid #d0d7d3; background: #fff; }
    .gate { min-height: 48px; display: flex; align-items: center; gap: 10px; padding: 10px 13px; border-bottom: 1px solid #e0e5e2; }
    .gate:nth-child(odd) { border-right: 1px solid #e0e5e2; }
    .gate:nth-last-child(-n+2) { border-bottom: 0; }
    .gate input { width: 17px; height: 17px; accent-color: #167653; }
    .gate span { font-size: 13px; }
    .gate.approval { background: #f6f8f7; }
    .actions { margin-top: 17px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .state { font-size: 12px; color: #7b5418; }
    .state.pass { color: #126c4b; font-weight: 650; }
    .save { background: #176c50; border-color: #176c50; color: #fff; min-width: 128px; }
    .save:hover { background: #12583f; border-color: #12583f; }
    .save:disabled { cursor: wait; opacity: .65; }
    .error { min-height: 18px; margin-top: 11px; color: #a1352d; font-size: 12px; }
    @media (max-width: 760px) {
      .topbar { height: auto; min-height: 64px; padding: 11px 14px; align-items: flex-start; }
      .layout { grid-template-columns: 1fr; height: auto; }
      .sidebar { border-right: 0; border-bottom: 1px solid #d2d9d5; max-height: 190px; }
      .workspace { padding: 18px 14px 30px; }
      .compare, .gates { grid-template-columns: 1fr; }
      .text-source { padding: 15px 0; }
      .text-source + .text-source { padding-left: 0; border-left: 0; border-top: 1px solid #ccd4d0; }
      .gate:nth-child(odd) { border-right: 0; }
      .gate:nth-last-child(2) { border-bottom: 1px solid #e0e5e2; }
      .metrics { grid-template-columns: 1fr 1fr 1fr; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div><h1>VoxCPM transcript review</h1><span class="digest" id="digest"></span></div>
    <div class="overall"><strong id="overallState">Loading queue</strong><span id="overallCount"></span></div>
  </header>
  <main class="layout">
    <aside class="sidebar">
      <div class="sidebar-head" id="sidebarHead">Review queue</div>
      <ol class="clip-list" id="clipList"></ol>
    </aside>
    <section class="workspace">
      <div class="workspace-inner">
        <div class="clip-toolbar">
          <div class="clip-heading"><h2 id="clipTitle">-</h2><span id="clipPosition"></span></div>
          <div class="nav"><button id="previous" title="Previous clip" aria-label="Previous clip">&larr;</button><button id="next" title="Next clip" aria-label="Next clip">&rarr;</button></div>
        </div>
        <div class="audio-band"><audio id="audio" controls preload="metadata"></audio></div>
        <div class="metrics">
          <div class="metric"><span>Duration</span><strong id="duration">-</strong></div>
          <div class="metric"><span id="captionLabel">OCR captions</span><strong id="captionCount">-</strong></div>
          <div class="metric"><span id="similarityLabel">ASR / OCR similarity</span><strong id="similarity">-</strong><div class="similarity-track"><div class="similarity-fill" id="similarityFill"></div></div></div>
        </div>
        <div class="compare">
          <section class="text-source"><h3>ASR candidate</h3><p class="source-text" id="asrText"></p></section>
          <section class="text-source"><h3 id="evidenceTitle">Burned subtitle OCR</h3><p class="source-text" id="ocrText"></p></section>
        </div>
        <section class="edit-section">
          <label class="title" for="correctedText">Corrected transcript</label>
          <textarea id="correctedText" maxlength="8000" spellcheck="false"></textarea>
          <fieldset>
            <legend>Review gates</legend>
            <div class="gates">
              <label class="gate"><input id="targetSpeaker" type="checkbox"><span>Target speaker only</span></label>
              <label class="gate"><input id="transcriptVerified" type="checkbox"><span>Transcript verified</span></label>
              <label class="gate"><input id="noThirdParty" type="checkbox"><span>No third-party speech</span></label>
              <label class="gate"><input id="noMusic" type="checkbox"><span>No background music</span></label>
              <label class="gate approval"><input id="approved" type="checkbox"><span>Approve clip</span></label>
            </div>
          </fieldset>
          <div class="actions"><span class="state" id="clipState">Pending review gates</span><button class="save" id="save">Save decision</button></div>
          <div class="error" id="error" role="status"></div>
        </section>
      </div>
    </section>
  </main>
  <script nonce="__NONCE__">
    'use strict';
    const csrfToken = __CSRF__;
    const elements = Object.fromEntries(['digest','overallState','overallCount','sidebarHead','clipList','clipTitle','clipPosition','previous','next','audio','duration','captionLabel','captionCount','similarityLabel','similarity','similarityFill','asrText','evidenceTitle','ocrText','correctedText','targetSpeaker','transcriptVerified','noThirdParty','noMusic','approved','clipState','save','error'].map(id => [id, document.getElementById(id)]));
    let state = null;
    let index = 0;

    function current() { return state.clips[index]; }
    function statusText(decision) { return decision.gates_satisfied ? 'Review gates satisfied' : 'Pending review gates'; }
    function renderOverall() {
      elements.overallState.textContent = state.all_review_gates_satisfied ? 'All review gates satisfied' : 'Review in progress';
      elements.overallCount.textContent = `${state.review_gates_satisfied} / ${state.total_clips} clips`;
      elements.sidebarHead.textContent = `${state.total_clips} clips`;
    }
    function renderList() {
      elements.clipList.replaceChildren();
      state.clips.forEach((clip, itemIndex) => {
        const row = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = itemIndex === index ? 'active' : '';
        button.addEventListener('click', () => { index = itemIndex; render(); });
        const number = document.createElement('span'); number.className = 'clip-index'; number.textContent = String(itemIndex + 1).padStart(2, '0');
        const name = document.createElement('span'); name.className = 'clip-name'; name.textContent = clip.clip_id;
        const marker = document.createElement('span'); marker.className = `clip-state${clip.decision.gates_satisfied ? ' pass' : ''}`;
        button.append(number, name, marker); row.append(button); elements.clipList.append(row);
      });
    }
    function render() {
      const clip = current();
      const decision = clip.decision;
      elements.clipTitle.textContent = clip.clip_id;
      elements.clipPosition.textContent = `Clip ${index + 1} of ${state.total_clips}`;
      elements.audio.src = clip.audio_url;
      elements.duration.textContent = `${clip.duration_seconds.toFixed(2)} s`;
      const ocrPerformed = state.review_evidence.ocr_performed;
      elements.captionLabel.textContent = ocrPerformed ? 'OCR captions' : 'OCR status';
      elements.captionCount.textContent = ocrPerformed ? String(clip.ocr_evidence.caption_count) : 'N/A';
      elements.similarityLabel.textContent = ocrPerformed ? 'ASR / OCR similarity' : 'Subtitle comparison';
      const percent = ocrPerformed ? Math.round(clip.ocr_evidence.similarity * 100) : 0;
      elements.similarity.textContent = ocrPerformed ? `${percent}%` : 'N/A';
      elements.similarityFill.style.width = `${percent}%`;
      elements.asrText.textContent = clip.candidate_transcript;
      elements.evidenceTitle.textContent = ocrPerformed ? 'Burned subtitle OCR' : 'Subtitle evidence';
      elements.ocrText.textContent = ocrPerformed ? (clip.ocr_evidence.subtitle_text || 'No OCR text') : 'Not applicable: audio-only source; OCR was not performed.';
      elements.correctedText.value = decision.corrected_text;
      elements.targetSpeaker.checked = decision.target_speaker_only;
      elements.transcriptVerified.checked = decision.transcript_verified;
      elements.noThirdParty.checked = decision.no_third_party_speech;
      elements.noMusic.checked = decision.no_background_music;
      elements.approved.checked = decision.approved;
      elements.clipState.textContent = statusText(decision);
      elements.clipState.className = `state${decision.gates_satisfied ? ' pass' : ''}`;
      elements.previous.disabled = index === 0;
      elements.next.disabled = index === state.clips.length - 1;
      elements.error.textContent = '';
      renderList(); renderOverall();
    }
    async function save() {
      const clip = current();
      const payload = {
        clip_id: clip.clip_id,
        corrected_text: elements.correctedText.value,
        target_speaker_only: elements.targetSpeaker.checked,
        transcript_verified: elements.transcriptVerified.checked,
        no_third_party_speech: elements.noThirdParty.checked,
        no_background_music: elements.noMusic.checked,
        approved: elements.approved.checked
      };
      elements.save.disabled = true; elements.error.textContent = '';
      try {
        const response = await fetch('/api/decisions', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(payload) });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Decision was rejected');
        clip.decision = result.decision;
        state.review_gates_satisfied = result.review_gates_satisfied;
        state.all_review_gates_satisfied = result.all_review_gates_satisfied;
        render();
      } catch (error) { elements.error.textContent = error.message; }
      finally { elements.save.disabled = false; }
    }
    elements.previous.addEventListener('click', () => { if (index > 0) { index -= 1; render(); } });
    elements.next.addEventListener('click', () => { if (index + 1 < state.clips.length) { index += 1; render(); } });
    elements.save.addEventListener('click', save);
    fetch('/api/queue').then(response => { if (!response.ok) throw new Error('Queue could not be loaded'); return response.json(); }).then(payload => { state = payload; elements.digest.textContent = `queue ${state.queue_sha256.slice(0, 12)}`; render(); }).catch(error => { elements.overallState.textContent = 'Queue unavailable'; elements.error.textContent = error.message; });
  </script>
</body>
</html>"""
    return (
        template.replace("__NONCE__", nonce)
        .replace("__CSRF__", csrf_json)
        .encode("utf-8")
    )


def create_server(
    *,
    queue_path: Path,
    decisions_path: Path,
    port: int = 0,
    access_token: str | None = None,
    csrf_token: str | None = None,
) -> ReviewHTTPServer:
    if type(port) is not int or not 0 <= port <= 65_535:
        raise ReviewToolError("port must be between 0 and 65535")
    queue = QueueSnapshot.load(queue_path)
    decisions = DecisionStore(decisions_path, queue)
    access_token = access_token or secrets.token_urlsafe(32)
    csrf_token = csrf_token or secrets.token_urlsafe(32)
    if len(access_token) < 32 or len(csrf_token) < 32:
        raise ReviewToolError("review tokens must contain at least 32 characters")
    return ReviewHTTPServer(
        ("127.0.0.1", port),
        queue,
        decisions,
        access_token=access_token,
        csrf_token=csrf_token,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        server = create_server(
            queue_path=args.queue,
            decisions_path=args.decisions,
            port=args.port,
        )
    except (OSError, ReviewToolError) as exc:
        raise SystemExit(f"review server failed: {exc}") from exc
    print(f"Review UI: {server.launch_url}", flush=True)
    print(f"Decisions: {server.decisions.path}", flush=True)
    if not args.no_browser:
        webbrowser.open(server.launch_url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
