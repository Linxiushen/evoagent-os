"""Reproducible, consent-gated qualification jobs for remote GPU workers.

This module deliberately keeps biometric media and credentials outside the
repository.  A private job manifest binds every input and model snapshot by
SHA-256, performs a hardware/runtime preflight, runs one pinned upstream path,
and emits a sanitized provenance document after independent metrics are added.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from echoweave import __version__

SCHEMA_VERSION = 1
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SECRET_FIELD_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|secret|password|access_?token|private_?key|credential)(?:$|_)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
RUN_ATTESTATION_KEY_ENV = "ECHOWEAVE_RUN_ATTESTATION_KEY"
RUN_ATTESTATION_ALGORITHM = "hmac-sha256"
AUTHORIZATION_RECORD_KIND = "echoweave_authorization_record"

VOXCPM_MODEL_ID = "openbmb/VoxCPM2"
VOXCPM_MODEL_REVISION = "bffb3df5a29440629464e5e839f4d214c8714c3d"
VOXCPM_REPO_ID = "OpenBMB/VoxCPM"
VOXCPM_REPO_REVISION = "616d3d3e630a9c96c2853250eef91b0f39dcd5fa"
VOXCPM_PACKAGE_VERSION = "2.0.3.post22+g616d3d3e6"
SOULX_MODEL_ID = "Soul-AILab/SoulX-FlashHead-1_3B"
SOULX_MODEL_REVISION = "59119b6c681230c3eeee157e224ae1941746711e"
SOULX_REPO_ID = "Soul-AILab/SoulX-FlashHead"
SOULX_REPO_REVISION = "9bc03de06bb0de82cd6bc477804512ae06144bf2"
WAV2VEC_MODEL_ID = "facebook/wav2vec2-base-960h"
WAV2VEC_MODEL_REVISION = "22aad52d435eb6dbaf354bdad9b0da84ce7d6156"

MODE_VOX_ZERO = "voxcpm2_zero_shot"
MODE_VOX_LORA = "voxcpm2_lora"
MODE_SOULX_LITE = "soulx_lite"
SUPPORTED_MODES = {MODE_VOX_ZERO, MODE_VOX_LORA, MODE_SOULX_LITE}

MODE_MODEL_SPECS = {
    MODE_VOX_ZERO: {VOXCPM_MODEL_ID: VOXCPM_MODEL_REVISION},
    MODE_VOX_LORA: {VOXCPM_MODEL_ID: VOXCPM_MODEL_REVISION},
    MODE_SOULX_LITE: {
        SOULX_MODEL_ID: SOULX_MODEL_REVISION,
        WAV2VEC_MODEL_ID: WAV2VEC_MODEL_REVISION,
    },
}
MODE_REPOSITORY_SPECS = {
    MODE_VOX_ZERO: {VOXCPM_REPO_ID: VOXCPM_REPO_REVISION},
    MODE_VOX_LORA: {VOXCPM_REPO_ID: VOXCPM_REPO_REVISION},
    MODE_SOULX_LITE: {SOULX_REPO_ID: SOULX_REPO_REVISION},
}
MODE_REQUIRED_SCOPES = {
    MODE_VOX_ZERO: {"voice_clone"},
    MODE_VOX_LORA: {"voice_clone"},
    MODE_SOULX_LITE: {"avatar_animation"},
}
MODE_REQUIRED_ROLES = {
    MODE_VOX_ZERO: {
        "authorization_record": (1, 1),
        "voice_reference_wav": (1, 1),
        "voice_reference_transcript": (1, 1),
        "synthesis_text": (1, 1),
    },
    MODE_VOX_LORA: {
        "authorization_record": (1, 1),
        "voice_reference_wav": (1, 1),
        "voice_reference_transcript": (1, 1),
        "synthesis_text": (1, 1),
        "voxcpm_dataset_manifest": (1, 1),
        "voxcpm_train_manifest": (1, 1),
        "voxcpm_train_config": (1, 1),
        "voxcpm_train_audio": (5, 10_000),
    },
    MODE_SOULX_LITE: {
        "authorization_record": (1, 1),
        "avatar_reference_png": (1, 1),
        "driving_audio_wav": (1, 1),
    },
}


class WorkerPackError(ValueError):
    """Raised when a remote worker job violates a reproducibility invariant."""


@dataclass(frozen=True)
class MetricRule:
    name: str
    operator: str
    threshold: float | bool


METRIC_PROFILES: dict[str, tuple[MetricRule, ...]] = {
    MODE_VOX_ZERO: (
        MetricRule("speaker_embedding_cosine", ">=", 0.82),
        MetricRule("asr_character_error_rate", "<=", 0.10),
        MetricRule("rtf_p95", "<=", 0.60),
        MetricRule("clipping_ratio", "<=", 0.001),
        MetricRule("sample_rate_hz", "==", 48_000.0),
        MetricRule("human_review_pass", "==", True),
        MetricRule("ai_disclosure_present", "==", True),
    ),
    MODE_VOX_LORA: (
        MetricRule("speaker_embedding_cosine", ">=", 0.84),
        MetricRule("asr_character_error_rate", "<=", 0.10),
        MetricRule("rtf_p95", "<=", 0.70),
        MetricRule("clipping_ratio", "<=", 0.001),
        MetricRule("sample_rate_hz", "==", 48_000.0),
        MetricRule("speaker_cosine_delta_vs_zero_shot", ">=", 0.02),
        MetricRule("cer_delta_vs_zero_shot", "<=", 0.02),
        MetricRule("human_review_pass", "==", True),
        MetricRule("ai_disclosure_present", "==", True),
    ),
    MODE_SOULX_LITE: (
        MetricRule("face_identity_cosine", ">=", 0.75),
        MetricRule("syncnet_lse_c", ">=", 5.0),
        MetricRule("fps", ">=", 25.0),
        MetricRule("rtf_p95", "<=", 1.0),
        MetricRule("dropped_frame_ratio", "<=", 0.001),
        MetricRule("watermark_present", "==", True),
        MetricRule("human_review_pass", "==", True),
        MetricRule("ai_disclosure_present", "==", True),
    ),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkerPackError(f"{field} must be an ISO-8601 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkerPackError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise WorkerPackError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkerPackError(f"cannot inspect JSON file: {path}") from exc
    if size <= 0 or size > maximum_bytes:
        raise WorkerPackError(f"JSON file size is invalid: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerPackError(f"invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkerPackError(f"JSON root must be an object: {path}")
    return payload


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_private_json(path: Path, payload: object) -> None:
    path = _assert_absolute_private_path(
        str(path), "private JSON output", must_exist=False
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise WorkerPackError(
                f"refusing to overwrite existing file: {path}"
            ) from exc
        except OSError as exc:
            raise WorkerPackError(
                f"cannot publish private JSON atomically: {path}"
            ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _assert_no_embedded_secrets(value: object, trail: str = "root") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            if SECRET_FIELD_PATTERN.search(key):
                raise WorkerPackError(
                    f"secret-like field is not permitted in a job manifest: {trail}.{key}"
                )
            _assert_no_embedded_secrets(child, f"{trail}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_embedded_secrets(child, f"{trail}[{index}]")
        return
    if isinstance(value, str) and SECRET_VALUE_PATTERN.search(value):
        raise WorkerPackError(f"secret-like value is not permitted at {trail}")


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _assert_absolute_private_path(
    raw: object,
    field: str,
    *,
    must_exist: bool,
    expect_directory: bool = False,
) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise WorkerPackError(f"{field} must be a non-empty absolute path")
    if raw.startswith(("http://", "https://", "hf://", "s3://", "gs://")):
        raise WorkerPackError(f"{field} must be a local mounted path, not a URL")
    path = Path(raw)
    if not path.is_absolute():
        raise WorkerPackError(f"{field} must be an absolute path")
    lexical = Path(os.path.abspath(path))
    if _inside(lexical, WORKSPACE_ROOT):
        raise WorkerPackError(
            f"{field} must be outside the source repository; use a private mount"
        )
    current = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        current /= component
        if os.path.lexists(current) and _is_link_like(current):
            raise WorkerPackError(f"{field} must not traverse a link or reparse point")
    if must_exist and not lexical.exists():
        raise WorkerPackError(f"{field} does not exist")
    if must_exist:
        try:
            metadata = lexical.lstat()
        except OSError as exc:
            raise WorkerPackError(f"cannot inspect {field}") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_link_like(lexical):
            raise WorkerPackError(f"{field} must not be a link or reparse point")
        if expect_directory and not stat.S_ISDIR(metadata.st_mode):
            raise WorkerPackError(f"{field} must be a directory")
        if not expect_directory and not stat.S_ISREG(metadata.st_mode):
            raise WorkerPackError(f"{field} must be a regular file")
    return lexical


def _sha256_file(path: Path, *, maximum_bytes: int | None = None) -> tuple[str, int]:
    before = path.stat()
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise WorkerPackError(f"file exceeds size limit: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise WorkerPackError(f"cannot hash file: {path.name}") from exc
    after = path.stat()
    stable = (before.st_size, before.st_mtime_ns, before.st_ino) == (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    )
    if not stable:
        raise WorkerPackError(f"file changed while hashing: {path.name}")
    return digest.hexdigest(), after.st_size


def _validate_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise WorkerPackError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise WorkerPackError(f"{field} is invalid")
    return value


def _validate_artifact(
    artifact: object,
    field: str,
    *,
    verify_file: bool,
) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise WorkerPackError(f"{field} must be an object")
    allowed = {"id", "role", "path", "sha256", "media_type"}
    unknown = set(artifact) - allowed
    if unknown:
        raise WorkerPackError(f"{field} contains unsupported fields: {sorted(unknown)}")
    artifact_id = _validate_identifier(artifact.get("id"), f"{field}.id")
    role = _validate_identifier(artifact.get("role"), f"{field}.role")
    digest = _validate_digest(artifact.get("sha256"), f"{field}.sha256")
    media_type = artifact.get("media_type")
    if (
        not isinstance(media_type, str)
        or not media_type.strip()
        or len(media_type) > 100
        or "\x00" in media_type
    ):
        raise WorkerPackError(f"{field}.media_type is invalid")
    path = _assert_absolute_private_path(
        artifact.get("path"),
        f"{field}.path",
        must_exist=verify_file,
    )
    size: int | None = None
    if verify_file:
        actual_digest, size = _sha256_file(path, maximum_bytes=2 * 1024**3)
        if actual_digest != digest:
            raise WorkerPackError(f"{field} SHA-256 does not match")
    return {
        "id": artifact_id,
        "role": role,
        "path": path,
        "sha256": digest,
        "media_type": media_type.strip().lower(),
        "size": size,
    }


def _snapshot_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if _is_link_like(path):
            raise WorkerPackError(
                f"model snapshot contains a symbolic link: {path.name}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise WorkerPackError(
                f"model snapshot contains a non-regular item: {path.name}"
            )
        digest, size = _sha256_file(path)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest,
                "size": size,
            }
        )
        if len(files) > 100_000:
            raise WorkerPackError("model snapshot exceeds the 100000-file limit")
    if not files:
        raise WorkerPackError("model snapshot directory is empty")
    return files


def create_model_snapshot(
    root: Path,
    *,
    model_id: str,
    revision: str,
    output: Path,
) -> dict[str, Any]:
    root = _assert_absolute_private_path(
        str(root), "root", must_exist=True, expect_directory=True
    )
    output = _assert_absolute_private_path(str(output), "output", must_exist=False)
    if _inside(output, root):
        raise WorkerPackError("snapshot manifest must be outside the model directory")
    expected = {
        **MODE_MODEL_SPECS[MODE_VOX_ZERO],
        **MODE_MODEL_SPECS[MODE_SOULX_LITE],
    }
    if expected.get(model_id) != revision:
        raise WorkerPackError("model ID/revision is not one of the audited snapshots")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "huggingface_model_snapshot",
        "model_id": model_id,
        "revision": revision,
        "files": _snapshot_files(root),
    }
    _write_private_json(output, payload)
    return payload


def _verify_model_snapshot(
    model: dict[str, Any], *, verify_files: bool
) -> dict[str, Any]:
    allowed = {"id", "revision", "path", "snapshot"}
    unknown = set(model) - allowed
    if unknown:
        raise WorkerPackError(f"model contains unsupported fields: {sorted(unknown)}")
    model_id = model.get("id")
    revision = model.get("revision")
    if not isinstance(model_id, str) or not isinstance(revision, str):
        raise WorkerPackError("model id and revision are required")
    root = _assert_absolute_private_path(
        model.get("path"),
        f"model[{model_id}].path",
        must_exist=verify_files,
        expect_directory=True,
    )
    snapshot = model.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {"path", "sha256"}:
        raise WorkerPackError(f"model[{model_id}].snapshot is invalid")
    snapshot_path = _assert_absolute_private_path(
        snapshot.get("path"),
        f"model[{model_id}].snapshot.path",
        must_exist=verify_files,
    )
    snapshot_digest = _validate_digest(
        snapshot.get("sha256"), f"model[{model_id}].snapshot.sha256"
    )
    file_count: int | None = None
    if verify_files:
        actual, _ = _sha256_file(snapshot_path, maximum_bytes=64 * 1024 * 1024)
        if actual != snapshot_digest:
            raise WorkerPackError(f"model[{model_id}] snapshot manifest hash mismatch")
        payload = _read_json(snapshot_path, maximum_bytes=64 * 1024 * 1024)
        if (
            payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("artifact_kind") != "huggingface_model_snapshot"
            or payload.get("model_id") != model_id
            or payload.get("revision") != revision
        ):
            raise WorkerPackError(f"model[{model_id}] snapshot identity mismatch")
        entries = payload.get("files")
        if not isinstance(entries, list) or not entries:
            raise WorkerPackError(f"model[{model_id}] snapshot has no files")
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
                raise WorkerPackError(
                    f"model[{model_id}] snapshot file {index} is invalid"
                )
            relative = entry.get("path")
            if not isinstance(relative, str):
                raise WorkerPackError(f"model[{model_id}] snapshot path is invalid")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or relative in seen:
                raise WorkerPackError(f"model[{model_id}] snapshot path is unsafe")
            seen.add(relative)
            expected_digest = _validate_digest(
                entry.get("sha256"), f"model[{model_id}] snapshot digest"
            )
            expected_size = entry.get("size")
            if not isinstance(expected_size, int) or expected_size < 0:
                raise WorkerPackError(f"model[{model_id}] snapshot size is invalid")
            path = root.joinpath(*pure.parts)
            if _is_link_like(path) or not path.is_file():
                raise WorkerPackError(f"model[{model_id}] snapshot file is missing")
            actual_digest, actual_size = _sha256_file(path)
            if actual_digest != expected_digest or actual_size != expected_size:
                raise WorkerPackError(f"model[{model_id}] snapshot file mismatch")
        actual_paths: set[str] = set()
        for path in root.rglob("*"):
            if _is_link_like(path):
                raise WorkerPackError(
                    f"model[{model_id}] contains an unmanifested link or reparse point"
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise WorkerPackError(
                    f"model[{model_id}] contains an unmanifested non-regular item"
                )
            actual_paths.add(path.relative_to(root).as_posix())
        if actual_paths != seen:
            raise WorkerPackError(f"model[{model_id}] contains unmanifested files")
        file_count = len(entries)
    return {
        "id": model_id,
        "revision": revision,
        "path": root,
        "snapshot_path": snapshot_path,
        "snapshot_sha256": snapshot_digest,
        "file_count": file_count,
    }


def _validate_models(
    job: dict[str, Any], mode: str, *, verify_files: bool
) -> list[dict[str, Any]]:
    raw_models = job.get("models")
    if not isinstance(raw_models, list):
        raise WorkerPackError("models must be a list")
    models = [
        _verify_model_snapshot(model, verify_files=verify_files)
        for model in raw_models
        if isinstance(model, dict)
    ]
    if len(models) != len(raw_models):
        raise WorkerPackError("every model must be an object")
    actual = {model["id"]: model["revision"] for model in models}
    if actual != MODE_MODEL_SPECS[mode]:
        raise WorkerPackError("model IDs/revisions do not match the audited mode lock")
    return models


def _validate_repositories(
    job: dict[str, Any], mode: str, *, verify_files: bool
) -> list[dict[str, Any]]:
    raw_repositories = job.get("source_repositories", [])
    if not isinstance(raw_repositories, list):
        raise WorkerPackError("source_repositories must be a list")
    repositories: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_repositories):
        if not isinstance(raw, dict) or set(raw) != {"id", "revision", "path"}:
            raise WorkerPackError(f"source_repositories[{index}] is invalid")
        repo_id = raw.get("id")
        revision = raw.get("revision")
        if not isinstance(repo_id, str) or not isinstance(revision, str):
            raise WorkerPackError(f"source_repositories[{index}] identity is invalid")
        path = _assert_absolute_private_path(
            raw.get("path"),
            f"source_repositories[{index}].path",
            must_exist=verify_files,
            expect_directory=True,
        )
        repositories.append({"id": repo_id, "revision": revision, "path": path})
    actual = {repo["id"]: repo["revision"] for repo in repositories}
    if actual != MODE_REPOSITORY_SPECS[mode]:
        raise WorkerPackError("source repository locks do not match the audited mode")
    return repositories


def _wav_info(path: Path) -> dict[str, float | int]:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            compression = handle.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise WorkerPackError(f"invalid WAV file: {path.name}") from exc
    if (
        compression != "NONE"
        or channels != 1
        or sample_width != 2
        or sample_rate != 16_000
    ):
        raise WorkerPackError(f"{path.name} must be mono PCM16 WAV at 16000 Hz")
    duration = frames / sample_rate if sample_rate else 0.0
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration_seconds": duration,
    }


def _validate_png(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as exc:
        raise WorkerPackError("cannot read avatar reference PNG") from exc
    if (
        len(header) < 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise WorkerPackError("avatar reference must be a valid PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if not (512 <= width <= 4096 and 512 <= height <= 4096):
        raise WorkerPackError("avatar reference PNG must be 512-4096 pixels per side")
    return width, height


def _artifact_by_role(
    artifacts: list[dict[str, Any]], role: str
) -> list[dict[str, Any]]:
    return [artifact for artifact in artifacts if artifact["role"] == role]


def _single_artifact(artifacts: list[dict[str, Any]], role: str) -> dict[str, Any]:
    matches = _artifact_by_role(artifacts, role)
    if len(matches) != 1:
        raise WorkerPackError(f"exactly one {role} artifact is required")
    return matches[0]


def _read_text_artifact(artifact: dict[str, Any], *, maximum_characters: int) -> str:
    try:
        text = artifact["path"].read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkerPackError(f"{artifact['role']} must be UTF-8 text") from exc
    if not text or len(text) > maximum_characters or "\x00" in text:
        raise WorkerPackError(f"{artifact['role']} text length is invalid")
    return text


def _validate_training_manifest(
    manifest_artifact: dict[str, Any],
    dataset_manifest_artifact: dict[str, Any],
    train_audio: list[dict[str, Any]],
) -> dict[str, Any]:
    dataset_root = manifest_artifact["path"].parent
    try:
        dataset_root_real = dataset_root.resolve(strict=True)
    except OSError as exc:
        raise WorkerPackError("VoxCPM dataset root cannot be resolved") from exc
    if (
        dataset_manifest_artifact["path"].name != "dataset-manifest.json"
        or dataset_manifest_artifact["path"].parent != dataset_root
    ):
        raise WorkerPackError(
            "dataset-manifest.json must be beside the VoxCPM training manifest"
        )

    def resolve_dataset_member(raw: object, field: str) -> tuple[Path, str]:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise WorkerPackError(f"{field} must be a non-empty local path")
        if raw.startswith(("http://", "https://", "hf://", "s3://", "gs://")):
            raise WorkerPackError(f"{field} must be a local dataset path, not a URL")
        raw_path = Path(raw)
        candidate = raw_path if raw_path.is_absolute() else dataset_root / raw_path
        candidate = Path(os.path.abspath(candidate))
        if not _inside(candidate, dataset_root):
            raise WorkerPackError(f"{field} escapes the VoxCPM dataset root")
        candidate = _assert_absolute_private_path(
            str(candidate), field, must_exist=True
        )
        current = dataset_root
        for component in candidate.relative_to(dataset_root).parts:
            current /= component
            is_junction = getattr(current, "is_junction", None)
            if current.is_symlink() or (callable(is_junction) and is_junction()):
                raise WorkerPackError(f"{field} must not traverse a link")
        try:
            real = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkerPackError(f"{field} cannot be resolved") from exc
        if not _inside(real, dataset_root_real):
            raise WorkerPackError(f"{field} real path escapes the VoxCPM dataset root")
        return candidate, candidate.relative_to(dataset_root).as_posix()

    inventory: dict[Path, dict[str, Any]] = {}
    inventory_relative: dict[Path, str] = {}
    for index, item in enumerate(train_audio):
        path, relative = resolve_dataset_member(
            str(item["path"]), f"VoxCPM train-audio inventory item {index}"
        )
        if path in inventory:
            raise WorkerPackError("VoxCPM train-audio inventory repeats a file")
        inventory[path] = item
        inventory_relative[path] = relative

    dataset_manifest = _read_json(dataset_manifest_artifact["path"])
    if (
        dataset_manifest.get("schema_version") != SCHEMA_VERSION
        or dataset_manifest.get("kind") != "echoweave-voxcpm2-training-dataset"
    ):
        raise WorkerPackError("VoxCPM dataset manifest identity is invalid")
    raw_derivations = dataset_manifest.get("derivations")
    if not isinstance(raw_derivations, list) or len(raw_derivations) > 10_000:
        raise WorkerPackError("VoxCPM dataset manifest derivations are invalid")
    derivations: dict[str, dict[str, Any]] = {}
    for index, raw_derivation in enumerate(raw_derivations):
        if not isinstance(raw_derivation, dict):
            raise WorkerPackError(
                f"VoxCPM dataset derivation {index} must be an object"
            )
        output_path = raw_derivation.get("output_path")
        if not isinstance(output_path, str) or Path(output_path).is_absolute():
            raise WorkerPackError(
                f"VoxCPM dataset derivation {index} output_path must be relative"
            )
        _, relative = resolve_dataset_member(
            output_path, f"VoxCPM dataset derivation {index} output_path"
        )
        digest = _validate_digest(
            raw_derivation.get("output_sha256"),
            f"VoxCPM dataset derivation {index} output_sha256",
        )
        size = raw_derivation.get("output_size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise WorkerPackError(
                f"VoxCPM dataset derivation {index} output_size_bytes is invalid"
            )
        if relative in derivations:
            raise WorkerPackError("VoxCPM dataset manifest repeats an output_path")
        derivations[relative] = {
            "sha256": digest,
            "size": size,
            "split": raw_derivation.get("split"),
        }

    referenced: set[Path] = set()
    total_duration = 0.0
    sample_count = 0
    try:
        lines = manifest_artifact["path"].read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkerPackError("VoxCPM training manifest must be UTF-8 JSONL") from exc
    if len(lines) > 10_000:
        raise WorkerPackError("VoxCPM training manifest exceeds 10000 samples")
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerPackError(
                f"VoxCPM training manifest line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(sample, dict):
            raise WorkerPackError(
                f"VoxCPM training manifest line {line_number} must be an object"
            )
        unknown = set(sample) - {
            "audio",
            "text",
            "ref_audio",
            "duration",
            "dataset_id",
        }
        if unknown:
            raise WorkerPackError(
                f"VoxCPM training line {line_number} has unsupported fields"
            )
        raw_audio = sample.get("audio")
        text = sample.get("text")
        try:
            audio, audio_relative = resolve_dataset_member(
                raw_audio, f"VoxCPM training line {line_number} audio"
            )
        except WorkerPackError as exc:
            raise WorkerPackError(
                f"VoxCPM training line {line_number} audio is invalid: {exc}"
            ) from exc
        if audio not in inventory:
            raise WorkerPackError(
                f"VoxCPM training line {line_number} audio is not hash-inventoried"
            )
        derivation = derivations.get(audio_relative)
        if derivation is None:
            raise WorkerPackError(
                f"VoxCPM training line {line_number} audio has no dataset derivation"
            )
        inventory_item = inventory[audio]
        inventory_size = inventory_item.get("size")
        if inventory_size is None:
            inventory_size = audio.stat().st_size
        if (
            derivation["sha256"] != inventory_item.get("sha256")
            or derivation["size"] != inventory_size
        ):
            raise WorkerPackError(
                f"VoxCPM training line {line_number} audio does not match its dataset derivation"
            )
        if derivation["split"] != "train":
            raise WorkerPackError(
                f"VoxCPM training line {line_number} derivation is not in the train split"
            )
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 5_000
            or "\x00" in text
        ):
            raise WorkerPackError(
                f"VoxCPM training line {line_number} transcript is invalid"
            )
        if audio in referenced:
            raise WorkerPackError(
                f"VoxCPM training line {line_number} repeats an audio file"
            )
        for key in ("ref_audio",):
            raw_reference = sample.get(key)
            if raw_reference is None:
                continue
            try:
                reference, _ = resolve_dataset_member(
                    raw_reference, f"VoxCPM training line {line_number} {key}"
                )
            except WorkerPackError as exc:
                raise WorkerPackError(
                    f"VoxCPM training line {line_number} {key} is invalid: {exc}"
                ) from exc
            if reference not in inventory:
                raise WorkerPackError(
                    f"VoxCPM training line {line_number} {key} is not hash-inventoried"
                )
        info = _wav_info(inventory[audio]["path"])
        duration = float(info["duration_seconds"])
        if not 3.0 <= duration <= 30.0:
            raise WorkerPackError(
                f"VoxCPM training clip on line {line_number} must be 3-30 seconds"
            )
        declared = sample.get("duration")
        if declared is not None and (
            not isinstance(declared, (int, float))
            or abs(float(declared) - duration) > 0.05
        ):
            raise WorkerPackError(
                f"VoxCPM training line {line_number} duration does not match WAV"
            )
        referenced.add(audio)
        total_duration += duration
        sample_count += 1
    if sample_count < 5 or total_duration < 300.0:
        raise WorkerPackError(
            "VoxCPM LoRA requires at least 5 clips and 300 seconds of authorized audio"
        )
    if referenced != set(inventory):
        raise WorkerPackError("VoxCPM train-audio inventory contains unused files")
    if {inventory_relative[path] for path in referenced} - set(derivations):
        raise WorkerPackError("VoxCPM train-audio inventory has no dataset derivation")
    return {"sample_count": sample_count, "duration_seconds": total_duration}


def _parse_lora_scalar(raw: str, field: str) -> str | int | float | bool | None:
    value = raw.strip()
    if not value:
        raise WorkerPackError(f"VoxCPM LoRA config field {field} has no value")
    if any(token in value for token in ("&", "*", "!", "|", ">", "{", "}", "[", "]")):
        raise WorkerPackError(
            f"VoxCPM LoRA config field {field} uses unsupported YAML syntax"
        )
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?(?:0|[1-9]\d*)", value):
        return int(value)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)(?:e[+-]?\d+)?", value, re.IGNORECASE):
        number = float(value)
        if not math.isfinite(number):
            raise WorkerPackError(f"VoxCPM LoRA config field {field} is not finite")
        return number
    if "#" in value or "\x00" in value:
        raise WorkerPackError(f"VoxCPM LoRA config field {field} is invalid")
    return value


def _parse_lora_config(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    section: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkerPackError("VoxCPM LoRA config must be UTF-8 YAML") from exc
    for line_number, raw in enumerate(lines, start=1):
        if "\t" in raw:
            raise WorkerPackError("VoxCPM LoRA config must not contain tabs")
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise WorkerPackError(
                f"VoxCPM LoRA config line {line_number} is not a mapping entry"
            )
        indentation = len(line) - len(line.lstrip())
        key, raw_value = line.strip().split(":", 1)
        if not key or "\x00" in key:
            raise WorkerPackError(
                f"VoxCPM LoRA config line {line_number} has an invalid key"
            )
        if indentation == 0:
            if not raw_value.strip():
                if key not in {"lambdas", "lora"}:
                    raise WorkerPackError(
                        f"VoxCPM LoRA config line {line_number} has an unsupported section"
                    )
                section = key
                continue
            section = None
            field = key
        elif indentation == 2 and section is not None:
            field = f"{section}.{key}"
        else:
            raise WorkerPackError(
                f"VoxCPM LoRA config line {line_number} has unsupported nesting"
            )
        if field in values:
            raise WorkerPackError(f"VoxCPM LoRA config repeats field {field}")
        values[field] = _parse_lora_scalar(raw_value, field)
    return values


def _validate_lora_config(
    path: Path,
    *,
    model_path: Path,
    train_manifest: Path,
    checkpoint_dir: Path,
) -> dict[str, Any]:
    config = _parse_lora_config(path)
    expected: dict[str, Any] = {
        "pretrained_path": str(model_path),
        "train_manifest": str(train_manifest),
        "val_manifest": None,
        "sample_rate": 16_000,
        "out_sample_rate": 48_000,
        "batch_size": 2,
        "grad_accum_steps": 8,
        "num_workers": 8,
        "num_iters": 1_000,
        "log_interval": 10,
        "valid_interval": 500,
        "save_interval": 500,
        "learning_rate": 0.0001,
        "weight_decay": 0.01,
        "warmup_steps": 100,
        "max_steps": 1_000,
        "max_batch_tokens": 8_192,
        "max_grad_norm": 1.0,
        "save_path": str(checkpoint_dir),
        "tensorboard": str(checkpoint_dir / "logs"),
        "lambdas.loss/diff": 1.0,
        "lambdas.loss/stop": 1.0,
        "lora.enable_lm": True,
        "lora.enable_dit": True,
        "lora.enable_proj": False,
        "lora.r": 32,
        "lora.alpha": 32,
        "lora.dropout": 0.0,
    }
    if set(config) != set(expected):
        missing = sorted(set(expected) - set(config))
        extra = sorted(set(config) - set(expected))
        raise WorkerPackError(
            f"VoxCPM LoRA config fields are not locked (missing={missing}, extra={extra})"
        )
    path_fields = {"pretrained_path", "train_manifest", "save_path", "tensorboard"}
    mismatched = sorted(
        key
        for key, value in expected.items()
        if (
            Path(str(config[key])) != Path(str(value))
            if key in path_fields
            else config[key] != value
        )
    )
    if mismatched:
        raise WorkerPackError(f"VoxCPM LoRA config values are not locked: {mismatched}")
    return config


def _validate_mode_inputs(
    job: dict[str, Any],
    mode: str,
    artifacts: list[dict[str, Any]],
    models: list[dict[str, Any]],
    repositories: list[dict[str, Any]],
    *,
    verify_files: bool,
) -> dict[str, Any]:
    allowed_roles = set(MODE_REQUIRED_ROLES[mode])
    actual_roles = {artifact["role"] for artifact in artifacts}
    if actual_roles - allowed_roles:
        raise WorkerPackError(
            f"unsupported input roles: {sorted(actual_roles - allowed_roles)}"
        )
    for role, (minimum, maximum) in MODE_REQUIRED_ROLES[mode].items():
        count = len(_artifact_by_role(artifacts, role))
        if not minimum <= count <= maximum:
            raise WorkerPackError(f"input role {role} has invalid count {count}")
    details: dict[str, Any] = {}
    if not verify_files:
        return details
    if mode in {MODE_VOX_ZERO, MODE_VOX_LORA}:
        reference = _single_artifact(artifacts, "voice_reference_wav")
        info = _wav_info(reference["path"])
        if not 3.0 <= float(info["duration_seconds"]) <= 30.0:
            raise WorkerPackError("voice reference must be 3-30 seconds")
        _read_text_artifact(
            _single_artifact(artifacts, "voice_reference_transcript"),
            maximum_characters=5_000,
        )
        _read_text_artifact(
            _single_artifact(artifacts, "synthesis_text"),
            maximum_characters=2_000,
        )
        details["voice_reference_duration_seconds"] = info["duration_seconds"]
    if mode == MODE_VOX_LORA:
        train_details = _validate_training_manifest(
            _single_artifact(artifacts, "voxcpm_train_manifest"),
            _single_artifact(artifacts, "voxcpm_dataset_manifest"),
            _artifact_by_role(artifacts, "voxcpm_train_audio"),
        )
        model_path = models[0]["path"]
        train_manifest = _single_artifact(artifacts, "voxcpm_train_manifest")["path"]
        execution = job["execution"]
        _validate_lora_config(
            _single_artifact(artifacts, "voxcpm_train_config")["path"],
            model_path=model_path,
            train_manifest=train_manifest,
            checkpoint_dir=Path(execution["checkpoint_dir"]),
        )
        details.update(train_details)
    if mode == MODE_SOULX_LITE:
        image = _single_artifact(artifacts, "avatar_reference_png")
        width, height = _validate_png(image["path"])
        info = _wav_info(_single_artifact(artifacts, "driving_audio_wav")["path"])
        if not 0.96 <= float(info["duration_seconds"]) <= 300.0:
            raise WorkerPackError("SoulX driving audio must be 0.96-300 seconds")
        details.update(
            {
                "avatar_width": width,
                "avatar_height": height,
                "driving_audio_duration_seconds": info["duration_seconds"],
            }
        )
    return details


def _validate_execution(
    job: dict[str, Any], mode: str, *, verify_files: bool
) -> dict[str, Any]:
    execution = job.get("execution")
    if not isinstance(execution, dict):
        raise WorkerPackError("execution must be an object")
    allowed = {"gpu_index", "output_dir", "output_filename", "checkpoint_dir", "seed"}
    unknown = set(execution) - allowed
    if unknown:
        raise WorkerPackError(
            f"execution contains unsupported fields: {sorted(unknown)}"
        )
    gpu_index = execution.get("gpu_index", 0)
    seed = execution.get("seed", 42)
    if not isinstance(gpu_index, int) or gpu_index < 0 or gpu_index > 64:
        raise WorkerPackError("execution.gpu_index is invalid")
    if not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise WorkerPackError("execution.seed is invalid")
    output_dir = _assert_absolute_private_path(
        execution.get("output_dir"),
        "execution.output_dir",
        must_exist=False,
        expect_directory=True,
    )
    output_filename = execution.get("output_filename")
    expected_suffix = ".mp4" if mode == MODE_SOULX_LITE else ".wav"
    if (
        not isinstance(output_filename, str)
        or Path(output_filename).name != output_filename
        or not IDENTIFIER_PATTERN.fullmatch(Path(output_filename).stem)
        or Path(output_filename).suffix.lower() != expected_suffix
    ):
        raise WorkerPackError(
            f"execution.output_filename must be a simple {expected_suffix} filename"
        )
    checkpoint_dir: Path | None = None
    if mode == MODE_VOX_LORA:
        checkpoint_dir = _assert_absolute_private_path(
            execution.get("checkpoint_dir"),
            "execution.checkpoint_dir",
            must_exist=False,
            expect_directory=True,
        )
        if (
            checkpoint_dir == output_dir
            or _inside(checkpoint_dir, output_dir)
            or _inside(output_dir, checkpoint_dir)
        ):
            raise WorkerPackError("checkpoint_dir and output_dir must be separate")
    elif "checkpoint_dir" in execution:
        raise WorkerPackError("checkpoint_dir is only valid for VoxCPM LoRA")
    if verify_files and output_dir.exists() and not output_dir.is_dir():
        raise WorkerPackError("execution.output_dir is not a directory")
    return {
        "gpu_index": gpu_index,
        "seed": seed,
        "output_dir": output_dir,
        "output_filename": output_filename,
        "checkpoint_dir": checkpoint_dir,
    }


def _validate_writable_path_separation(
    execution: dict[str, Any],
    artifacts: list[dict[str, Any]],
    models: list[dict[str, Any]],
    repositories: list[dict[str, Any]],
    manifest_path: Path,
) -> None:
    writable = [execution["output_dir"]]
    if execution.get("checkpoint_dir") is not None:
        writable.append(execution["checkpoint_dir"])
    protected = [manifest_path]
    protected.extend(artifact["path"] for artifact in artifacts)
    for model in models:
        protected.extend((model["path"], model["snapshot_path"]))
    protected.extend(repository["path"] for repository in repositories)
    for writable_path in writable:
        for protected_path in protected:
            if (
                writable_path == protected_path
                or _inside(writable_path, protected_path)
                or _inside(protected_path, writable_path)
            ):
                raise WorkerPackError(
                    "execution output/checkpoint paths must be separate from all immutable inputs"
                )


def _validate_authorization(
    job: dict[str, Any],
    mode: str,
    artifacts: list[dict[str, Any]],
    *,
    now: datetime,
    verify_files: bool,
) -> dict[str, Any]:
    authorization = job.get("authorization")
    if not isinstance(authorization, dict):
        raise WorkerPackError("authorization must be an object")
    required_fields = {
        "record_id",
        "record_sha256",
        "status",
        "subject_verified",
        "authority_verified",
        "approved_at",
        "valid_until",
        "scopes",
        "allowed_processors",
    }
    if set(authorization) != required_fields:
        raise WorkerPackError("authorization fields do not match the required schema")
    record_id = _validate_identifier(
        authorization.get("record_id"), "authorization.record_id"
    )
    record_sha256 = _validate_digest(
        authorization.get("record_sha256"), "authorization.record_sha256"
    )
    record_artifact = _single_artifact(artifacts, "authorization_record")
    if record_artifact["sha256"] != record_sha256:
        raise WorkerPackError("authorization record digest does not match its artifact")
    if record_artifact["media_type"] != "application/json":
        raise WorkerPackError("authorization record must be application/json")
    record_payload = _read_json(record_artifact["path"]) if verify_files else None
    if authorization.get("status") != "approved":
        raise WorkerPackError("authorization.status must be approved")
    if authorization.get("subject_verified") is not True:
        raise WorkerPackError("authorization subject identity is not verified")
    if authorization.get("authority_verified") is not True:
        raise WorkerPackError("authorization authority is not verified")
    approved_at = _parse_utc(
        authorization.get("approved_at"), "authorization.approved_at"
    )
    valid_until = _parse_utc(
        authorization.get("valid_until"), "authorization.valid_until"
    )
    if approved_at > now or valid_until <= now or approved_at >= valid_until:
        raise WorkerPackError("authorization is not currently valid")
    scopes = authorization.get("scopes")
    if not isinstance(scopes, list) or any(
        not isinstance(item, str) for item in scopes
    ):
        raise WorkerPackError("authorization.scopes must be a string list")
    if not MODE_REQUIRED_SCOPES[mode] <= set(scopes):
        raise WorkerPackError("authorization does not cover the requested worker mode")
    processors = authorization.get("allowed_processors")
    expected_processors = set(MODE_MODEL_SPECS[mode])
    if (
        not isinstance(processors, list)
        or any(not isinstance(item, str) for item in processors)
        or not expected_processors <= set(processors)
    ):
        raise WorkerPackError("authorization does not name every model processor")
    if record_payload is not None:
        record_fields = (required_fields - {"record_sha256"}) | {
            "schema_version",
            "artifact_kind",
        }
        if set(record_payload) != record_fields:
            raise WorkerPackError(
                "authorization record artifact fields do not match the required schema"
            )
        if (
            record_payload.get("schema_version") != SCHEMA_VERSION
            or record_payload.get("artifact_kind") != AUTHORIZATION_RECORD_KIND
        ):
            raise WorkerPackError("authorization record artifact identity is invalid")
        scalar_fields = (
            "record_id",
            "status",
            "subject_verified",
            "authority_verified",
        )
        if any(
            record_payload.get(field) != authorization.get(field)
            for field in scalar_fields
        ):
            raise WorkerPackError(
                "authorization record artifact does not match the job authorization"
            )
        record_approved_at = _parse_utc(
            record_payload.get("approved_at"), "authorization record approved_at"
        )
        record_valid_until = _parse_utc(
            record_payload.get("valid_until"), "authorization record valid_until"
        )
        if record_approved_at != approved_at or record_valid_until != valid_until:
            raise WorkerPackError(
                "authorization record validity does not match the job authorization"
            )
        record_scopes = record_payload.get("scopes")
        record_processors = record_payload.get("allowed_processors")
        if (
            not isinstance(record_scopes, list)
            or any(not isinstance(item, str) for item in record_scopes)
            or set(record_scopes) != set(scopes)
            or not isinstance(record_processors, list)
            or any(not isinstance(item, str) for item in record_processors)
            or set(record_processors) != set(processors)
        ):
            raise WorkerPackError(
                "authorization record scopes or processors do not match the job authorization"
            )
    return {
        "record_id": record_id,
        "record_sha256": record_sha256,
        "approved_at": _iso_utc(approved_at),
        "valid_until": _iso_utc(valid_until),
        "scopes": sorted(set(scopes)),
        "allowed_processors": sorted(set(processors)),
    }


def validate_job(
    manifest_path: Path,
    *,
    verify_files: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    manifest_path = _assert_absolute_private_path(
        str(manifest_path), "manifest", must_exist=True
    )
    job = _read_json(manifest_path)
    _assert_no_embedded_secrets(job)
    allowed_top_level = {
        "schema_version",
        "run_id",
        "mode",
        "authorization",
        "models",
        "source_repositories",
        "inputs",
        "execution",
    }
    unknown = set(job) - allowed_top_level
    if unknown:
        raise WorkerPackError(f"job contains unsupported fields: {sorted(unknown)}")
    if job.get("schema_version") != SCHEMA_VERSION:
        raise WorkerPackError("unsupported job schema_version")
    run_id = _validate_identifier(job.get("run_id"), "run_id")
    mode = job.get("mode")
    if mode not in SUPPORTED_MODES:
        raise WorkerPackError("unsupported worker mode")
    execution = _validate_execution(job, mode, verify_files=verify_files)
    raw_artifacts = job.get("inputs")
    if not isinstance(raw_artifacts, list):
        raise WorkerPackError("inputs must be a list")
    artifacts = [
        _validate_artifact(item, f"inputs[{index}]", verify_file=verify_files)
        for index, item in enumerate(raw_artifacts)
    ]
    ids = [artifact["id"] for artifact in artifacts]
    paths = [str(artifact["path"]) for artifact in artifacts]
    if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
        raise WorkerPackError("input artifact IDs and paths must be unique")
    models = _validate_models(job, mode, verify_files=verify_files)
    repositories = _validate_repositories(job, mode, verify_files=verify_files)
    _validate_writable_path_separation(
        execution, artifacts, models, repositories, manifest_path
    )
    authorization = _validate_authorization(
        job,
        mode,
        artifacts,
        now=(now or _utc_now()).astimezone(timezone.utc),
        verify_files=verify_files,
    )
    mode_details = _validate_mode_inputs(
        job,
        mode,
        artifacts,
        models,
        repositories,
        verify_files=verify_files,
    )
    manifest_sha256, manifest_size = _sha256_file(manifest_path)
    return {
        "path": manifest_path,
        "sha256": manifest_sha256,
        "size": manifest_size,
        "run_id": run_id,
        "mode": mode,
        "authorization": authorization,
        "artifacts": artifacts,
        "models": models,
        "repositories": repositories,
        "execution": execution,
        "mode_details": mode_details,
    }


def _verify_repository_state(repository: dict[str, Any]) -> None:
    path = _assert_absolute_private_path(
        str(repository["path"]),
        f"source repository {repository['id']}",
        must_exist=True,
        expect_directory=True,
    )
    revision = _run_capture(["git", "rev-parse", "HEAD"], cwd=path)
    clean = _run_capture(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=path
    )
    if revision.returncode != 0 or revision.stdout.strip() != repository["revision"]:
        raise WorkerPackError(
            f"source repository {repository['id']} changed after validation"
        )
    if clean.returncode != 0 or clean.stdout.strip():
        raise WorkerPackError(
            f"source repository {repository['id']} is not clean at use time"
        )


def _verify_validated_binding(
    validated: dict[str, Any], *, phase: str
) -> dict[str, Any]:
    manifest_digest, manifest_size = _sha256_file(validated["path"])
    if manifest_digest != validated["sha256"] or manifest_size != validated["size"]:
        raise WorkerPackError(f"job manifest changed before {phase}")
    if (
        _parse_utc(validated["authorization"]["valid_until"], "valid_until")
        <= _utc_now()
    ):
        raise WorkerPackError(f"authorization expired before {phase}")

    artifact_bindings: list[dict[str, Any]] = []
    for artifact in validated["artifacts"]:
        path = _assert_absolute_private_path(
            str(artifact["path"]),
            f"artifact {artifact['id']} at {phase}",
            must_exist=True,
        )
        digest, size = _sha256_file(path, maximum_bytes=2 * 1024**3)
        if digest != artifact["sha256"] or size != artifact["size"]:
            raise WorkerPackError(f"artifact {artifact['id']} changed before {phase}")
        artifact_bindings.append(
            {
                "id": artifact["id"],
                "role": artifact["role"],
                "sha256": digest,
                "size": size,
            }
        )

    model_bindings: list[dict[str, Any]] = []
    for model in validated["models"]:
        verified = _verify_model_snapshot(
            {
                "id": model["id"],
                "revision": model["revision"],
                "path": str(model["path"]),
                "snapshot": {
                    "path": str(model["snapshot_path"]),
                    "sha256": model["snapshot_sha256"],
                },
            },
            verify_files=True,
        )
        model_bindings.append(
            {
                "id": verified["id"],
                "revision": verified["revision"],
                "snapshot_sha256": verified["snapshot_sha256"],
                "file_count": verified["file_count"],
            }
        )

    repository_bindings: list[dict[str, Any]] = []
    for repository in validated["repositories"]:
        _verify_repository_state(repository)
        repository_bindings.append(
            {"id": repository["id"], "revision": repository["revision"]}
        )

    if validated["mode"] in {MODE_VOX_ZERO, MODE_VOX_LORA}:
        repository = next(
            repo for repo in validated["repositories"] if repo["id"] == VOXCPM_REPO_ID
        )
        source_root = (repository["path"] / "src").resolve(strict=True)
        module_origin = _module_origin("voxcpm")
        if module_origin is None or not module_origin.is_relative_to(source_root):
            raise WorkerPackError("voxcpm is not imported from the audited checkout")

    return {
        "phase": phase,
        "verified_at": _iso_utc(),
        "manifest_sha256": manifest_digest,
        "artifacts": artifact_bindings,
        "models": model_bindings,
        "source_repositories": repository_bindings,
    }


def _run_attestation_key() -> bytes:
    raw = os.environ.get(RUN_ATTESTATION_KEY_ENV, "")
    key = raw.encode("utf-8")
    if len(key) < 32:
        raise WorkerPackError(
            f"{RUN_ATTESTATION_KEY_ENV} must contain at least 32 UTF-8 bytes"
        )
    return key


def _attestation_message(record: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in record.items() if key != "attestation"}
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _attach_run_attestation(record: dict[str, Any], key: bytes) -> dict[str, Any]:
    signed = dict(record)
    signed["attestation"] = {
        "algorithm": RUN_ATTESTATION_ALGORITHM,
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "signature": hmac.new(
            key, _attestation_message(record), hashlib.sha256
        ).hexdigest(),
    }
    return signed


def _verify_run_attestation(record: dict[str, Any], key: bytes) -> None:
    attestation = record.get("attestation")
    if not isinstance(attestation, dict) or set(attestation) != {
        "algorithm",
        "key_id",
        "signature",
    }:
        raise WorkerPackError("run record has no valid attestation")
    expected_key_id = hashlib.sha256(key).hexdigest()[:16]
    signature = attestation.get("signature")
    expected = hmac.new(key, _attestation_message(record), hashlib.sha256).hexdigest()
    if (
        attestation.get("algorithm") != RUN_ATTESTATION_ALGORITHM
        or attestation.get("key_id") != expected_key_id
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, expected)
    ):
        raise WorkerPackError("run record attestation is invalid")


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _module_origin(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or not isinstance(spec.origin, str):
        return None
    try:
        return Path(spec.origin).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _run_capture(
    command: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, "", type(exc).__name__)


def _gpu_inventory() -> list[dict[str, Any]]:
    result = _run_capture(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.free,driver_version,compute_cap",
            "--format=csv,noheader,nounits",
        ]
    )
    if result.returncode != 0:
        return []
    inventory: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6:
            continue
        try:
            inventory.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_total_mib": int(parts[2]),
                    "memory_free_mib": int(parts[3]),
                    "driver_version": parts[4],
                    "compute_capability": parts[5],
                }
            )
        except ValueError:
            continue
    return inventory


def _supports_native_bfloat16(gpu: dict[str, Any] | None) -> bool:
    if gpu is None:
        return False
    value = gpu.get("compute_capability")
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        return False
    major, minor = (int(part) for part in value.split(".", maxsplit=1))
    return (major, minor) >= (8, 0)


def _check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    detail: str,
) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def preflight_job(validated: dict[str, Any]) -> dict[str, Any]:
    mode = validated["mode"]
    checks: list[dict[str, Any]] = []
    inventory = _gpu_inventory()
    gpu_index = validated["execution"]["gpu_index"]
    gpu = next((item for item in inventory if item["index"] == gpu_index), None)
    minimum_total = 8 * 1024 if mode == MODE_VOX_ZERO else 24 * 1024
    minimum_free = 7 * 1024 if mode == MODE_VOX_ZERO else 22 * 1024
    _check(checks, "nvidia_gpu", gpu is not None, "selected GPU is visible")
    _check(
        checks,
        "gpu_memory_total",
        gpu is not None and gpu["memory_total_mib"] >= minimum_total,
        f"requires at least {minimum_total // 1024} GiB total VRAM",
    )
    _check(
        checks,
        "gpu_memory_free",
        gpu is not None and gpu["memory_free_mib"] >= minimum_free,
        f"requires at least {minimum_free // 1024} GiB free VRAM at admission",
    )
    if mode in {MODE_VOX_ZERO, MODE_VOX_LORA}:
        _check(
            checks,
            "gpu_native_bfloat16",
            _supports_native_bfloat16(gpu),
            "the audited VoxCPM2 path requires an Ampere-or-newer GPU "
            "with native bfloat16 support",
        )
    if mode == MODE_VOX_LORA:
        _check(
            checks,
            "linux",
            sys.platform.startswith("linux"),
            "the audited LoRA worker requires Linux; upstream Windows training "
            "is not qualified",
        )
    _check(
        checks,
        "python_version",
        (3, 10) <= sys.version_info[:2] < (3, 13),
        "requires Python >=3.10,<3.13",
    )
    torch_version = _package_version("torch")
    _check(
        checks,
        "torch",
        torch_version == "2.7.1",
        "the audited worker requires torch==2.7.1",
    )
    torch_cuda_available = False
    torch_cuda_version: str | None = None
    if torch_version is not None:
        try:
            import torch

            torch_cuda_available = bool(torch.cuda.is_available())
            torch_cuda_version = torch.version.cuda
        except (ImportError, RuntimeError):
            torch_cuda_available = False
    _check(
        checks,
        "torch_cuda",
        torch_cuda_available,
        "the installed PyTorch build must expose CUDA",
    )
    _check(
        checks,
        "torch_cuda_version",
        torch_cuda_version == "12.8",
        "the audited worker requires the CUDA 12.8 PyTorch build",
    )
    voxcpm_version = _package_version("voxcpm")
    if mode in {MODE_VOX_ZERO, MODE_VOX_LORA}:
        _check(
            checks,
            "voxcpm",
            voxcpm_version == VOXCPM_PACKAGE_VERSION,
            f"the audited worker requires voxcpm=={VOXCPM_PACKAGE_VERSION}",
        )
        voxcpm_repository = next(
            (
                repository
                for repository in validated["repositories"]
                if repository["id"] == VOXCPM_REPO_ID
            ),
            None,
        )
        if voxcpm_repository is not None:
            source_root = (voxcpm_repository["path"] / "src").resolve()
            module_origin = _module_origin("voxcpm")
            _check(
                checks,
                "voxcpm_source_checkout",
                module_origin is not None and module_origin.is_relative_to(source_root),
                "voxcpm must import from the audited source checkout",
            )
        _check(
            checks,
            "soundfile",
            _package_version("soundfile") == "0.13.1",
            "the audited worker requires soundfile==0.13.1",
        )
    if mode == MODE_SOULX_LITE:
        _check(
            checks, "linux", sys.platform.startswith("linux"), "SoulX requires Linux"
        )
        _check(
            checks, "ffmpeg", shutil.which("ffmpeg") is not None, "ffmpeg is required"
        )
        ffmpeg_filters = _run_capture(["ffmpeg", "-hide_banner", "-filters"])
        _check(
            checks,
            "ffmpeg_drawtext",
            ffmpeg_filters.returncode == 0 and "drawtext" in ffmpeg_filters.stdout,
            "ffmpeg must include drawtext for permanent AI disclosure",
        )
        _check(
            checks,
            "torchvision_version",
            _package_version("torchvision") == "0.22.1",
            "the audited SoulX environment requires torchvision==0.22.1",
        )
        _check(
            checks,
            "flash_attn",
            _package_version("flash-attn") == "2.8.0.post2",
            "flash-attn 2.8.0.post2 is required by the audited worker",
        )
    required_model_files = {
        VOXCPM_MODEL_ID: ("config.json", "model.safetensors", "audiovae.pth"),
        SOULX_MODEL_ID: (
            "Model_Lite/config.json",
            "Model_Lite/diffusion_pytorch_model.safetensors",
            "VAE_LTX/config.json",
            "VAE_LTX/diffusion_pytorch_model.safetensors",
        ),
        WAV2VEC_MODEL_ID: ("config.json",),
    }
    for model in validated["models"]:
        for relative in required_model_files[model["id"]]:
            _check(
                checks,
                f"model_file:{model['id']}:{relative}",
                (model["path"] / relative).is_file(),
                "required file must exist in the hash-verified snapshot",
            )
        if model["id"] == WAV2VEC_MODEL_ID:
            _check(
                checks,
                "wav2vec_weights",
                any(
                    (model["path"] / name).is_file()
                    for name in ("model.safetensors", "pytorch_model.bin")
                ),
                "wav2vec snapshot must contain model weights",
            )
    for repository in validated["repositories"]:
        revision = _run_capture(["git", "rev-parse", "HEAD"], cwd=repository["path"])
        clean = _run_capture(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository["path"],
        )
        _check(
            checks,
            f"repository_revision:{repository['id']}",
            revision.returncode == 0
            and revision.stdout.strip() == repository["revision"],
            "checked-out source revision must equal the audited commit",
        )
        _check(
            checks,
            f"repository_clean:{repository['id']}",
            clean.returncode == 0 and not clean.stdout.strip(),
            "tracked source files must be unmodified",
        )
        required_source = repository["path"] / (
            "scripts/train_voxcpm_finetune.py"
            if repository["id"] == VOXCPM_REPO_ID
            else "generate_video.py"
        )
        _check(
            checks,
            f"repository_entrypoint:{repository['id']}",
            required_source.is_file(),
            "pinned upstream entrypoint must exist",
        )
    output = (
        validated["execution"]["output_dir"] / validated["execution"]["output_filename"]
    )
    _check(
        checks,
        "output_absent",
        not output.exists(),
        "output file must not already exist",
    )
    checkpoint = validated["execution"].get("checkpoint_dir")
    if checkpoint is not None:
        _check(
            checks,
            "checkpoint_absent",
            not checkpoint.exists(),
            "LoRA checkpoint directory must be new for a reproducible run",
        )
    versions = {
        "echoweave": __version__,
        "python": platform.python_version(),
        "torch": torch_version,
        "torch_cuda": torch_cuda_version,
        "voxcpm": voxcpm_version,
        "flash_attn": _package_version("flash-attn"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": validated["run_id"],
        "mode": mode,
        "checked_at": _iso_utc(),
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "gpu": gpu,
        "versions": versions,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
    }


def _offline_environment(gpu_index: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(RUN_ATTESTATION_KEY_ENV, None)
    environment.update(
        {
            "CUDA_VISIBLE_DEVICES": str(gpu_index),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
        }
    )
    return environment


def _temporary_media_path(destination: Path) -> Path:
    return destination.with_name(
        f".{destination.stem}.{os.getpid()}.{secrets.token_hex(8)}.tmp{destination.suffix}"
    )


def _publish_new_file(temporary: Path, destination: Path) -> Path:
    destination = _assert_absolute_private_path(
        str(destination), "media output", must_exist=False
    )
    if _is_link_like(temporary) or not temporary.is_file():
        raise WorkerPackError("temporary media output is not a regular file")
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise WorkerPackError(
            f"refusing to overwrite existing output: {destination}"
        ) from exc
    except OSError as exc:
        raise WorkerPackError(
            f"cannot publish media output atomically: {destination}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _run_voxcpm(validated: dict[str, Any], *, lora_checkpoint: Path | None) -> Path:
    phase = (
        "VoxCPM LoRA inference" if lora_checkpoint is not None else "VoxCPM inference"
    )
    _verify_validated_binding(validated, phase=phase)
    try:
        import soundfile as sf
        from voxcpm.core import VoxCPM
    except ImportError as exc:
        raise WorkerPackError("VoxCPM runtime is unavailable") from exc
    artifacts = validated["artifacts"]
    model_path = validated["models"][0]["path"]
    reference = _single_artifact(artifacts, "voice_reference_wav")["path"]
    prompt_text = _read_text_artifact(
        _single_artifact(artifacts, "voice_reference_transcript"),
        maximum_characters=5_000,
    )
    target_text = _read_text_artifact(
        _single_artifact(artifacts, "synthesis_text"), maximum_characters=2_000
    )
    kwargs: dict[str, Any] = {
        "hf_model_id": str(model_path),
        "load_denoiser": False,
        "optimize": True,
    }
    if lora_checkpoint is not None:
        try:
            from voxcpm.model.voxcpm import LoRAConfig
        except ImportError as exc:
            raise WorkerPackError("VoxCPM LoRA runtime is unavailable") from exc
        lora_info = _read_json(lora_checkpoint / "lora_config.json")
        raw_config = lora_info.get("lora_config")
        if not isinstance(raw_config, dict):
            raise WorkerPackError("LoRA checkpoint config is invalid")
        kwargs["lora_config"] = LoRAConfig(**raw_config)
        kwargs["lora_weights_path"] = str(lora_checkpoint)
    model = VoxCPM.from_pretrained(**kwargs)
    audio = model.generate(
        text=target_text,
        reference_wav_path=str(reference),
        prompt_wav_path=str(reference),
        prompt_text=prompt_text,
        cfg_value=2.0,
        inference_timesteps=10,
        denoise=False,
        seed=validated["execution"]["seed"],
    )
    output = (
        validated["execution"]["output_dir"] / validated["execution"]["output_filename"]
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = _temporary_media_path(output)
    try:
        sf.write(str(temporary), audio, model.tts_model.sample_rate, subtype="PCM_16")
        _verify_validated_binding(validated, phase=f"{phase} completion")
        return _publish_new_file(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _run_lora_training(validated: dict[str, Any]) -> Path:
    _verify_validated_binding(validated, phase="VoxCPM LoRA training")
    repository = validated["repositories"][0]["path"]
    config = _single_artifact(validated["artifacts"], "voxcpm_train_config")["path"]
    dataset_root = _single_artifact(validated["artifacts"], "voxcpm_train_manifest")[
        "path"
    ].parent
    command = [
        sys.executable,
        str(repository / "scripts" / "train_voxcpm_finetune.py"),
        "--config_path",
        str(config),
    ]
    checkpoint_root = validated["execution"]["checkpoint_dir"]
    try:
        checkpoint_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise WorkerPackError(
            "LoRA checkpoint directory appeared after preflight"
        ) from exc
    result = subprocess.run(
        command,
        cwd=dataset_root,
        env=_offline_environment(validated["execution"]["gpu_index"]),
        check=False,
    )
    if result.returncode != 0:
        raise WorkerPackError("VoxCPM LoRA training process failed")
    _verify_validated_binding(validated, phase="VoxCPM LoRA training completion")
    checkpoint = checkpoint_root / "latest"
    if not (checkpoint / "lora_config.json").is_file():
        raise WorkerPackError(
            "VoxCPM LoRA training did not produce a latest checkpoint"
        )
    if not any(
        (checkpoint / name).is_file()
        for name in ("lora_weights.safetensors", "lora_weights.ckpt")
    ):
        raise WorkerPackError("VoxCPM LoRA checkpoint has no weights")
    return checkpoint


def _run_soulx(validated: dict[str, Any]) -> Path:
    _verify_validated_binding(validated, phase="SoulX inference")
    repository = validated["repositories"][0]["path"]
    model_by_id = {model["id"]: model for model in validated["models"]}
    image = _single_artifact(validated["artifacts"], "avatar_reference_png")["path"]
    audio = _single_artifact(validated["artifacts"], "driving_audio_wav")["path"]
    output = (
        validated["execution"]["output_dir"] / validated["execution"]["output_filename"]
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_output = output.with_name(
        f".{output.stem}.{os.getpid()}.{secrets.token_hex(8)}.unwatermarked.mp4"
    )
    watermarked_output = _temporary_media_path(output)
    command = [
        sys.executable,
        str(repository / "generate_video.py"),
        "--ckpt_dir",
        str(model_by_id[SOULX_MODEL_ID]["path"]),
        "--wav2vec_dir",
        str(model_by_id[WAV2VEC_MODEL_ID]["path"]),
        "--model_type",
        "lite",
        "--cond_image",
        str(image),
        "--audio_path",
        str(audio),
        "--audio_encode_mode",
        "stream",
        "--base_seed",
        str(validated["execution"]["seed"]),
        "--save_file",
        str(raw_output),
    ]
    result = subprocess.run(
        command,
        cwd=repository,
        env=_offline_environment(validated["execution"]["gpu_index"]),
        check=False,
    )
    if result.returncode != 0 or not raw_output.is_file():
        raw_output.unlink(missing_ok=True)
        raise WorkerPackError("SoulX Lite inference process failed")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raw_output.unlink(missing_ok=True)
        raise WorkerPackError("ffmpeg is unavailable for SoulX disclosure watermarking")
    watermark = (
        "drawtext=text='AI DIGITAL TWIN':x=w-tw-24:y=h-th-24:"
        "fontcolor=white:fontsize=h/28:box=1:boxcolor=black@0.65:boxborderw=10"
    )
    watermarked = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(raw_output),
            "-vf",
            watermark,
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
            str(watermarked_output),
        ],
        check=False,
    )
    raw_output.unlink(missing_ok=True)
    if watermarked.returncode != 0 or not watermarked_output.is_file():
        watermarked_output.unlink(missing_ok=True)
        raise WorkerPackError("SoulX disclosure watermarking failed")
    _verify_validated_binding(validated, phase="SoulX inference completion")
    return _publish_new_file(watermarked_output, output)


def _output_record(path: Path, kind: str) -> dict[str, Any]:
    path = path.resolve(strict=True)
    digest, size = _sha256_file(path)
    return {
        "kind": kind,
        "path": str(path),
        "sha256": digest,
        "size": size,
    }


def run_job(validated: dict[str, Any], record_path: Path) -> dict[str, Any]:
    record_path = _assert_absolute_private_path(
        str(record_path), "record", must_exist=False
    )
    attestation_key = _run_attestation_key()
    input_binding = _verify_validated_binding(validated, phase="job execution")
    os.environ.update(_offline_environment(validated["execution"]["gpu_index"]))
    preflight = preflight_job(validated)
    if not preflight["ok"]:
        failed = [item["name"] for item in preflight["checks"] if not item["ok"]]
        raise WorkerPackError(f"preflight failed: {', '.join(failed)}")
    started_at = _iso_utc()
    checkpoint: Path | None = None
    if validated["mode"] == MODE_VOX_ZERO:
        output = _run_voxcpm(validated, lora_checkpoint=None)
    elif validated["mode"] == MODE_VOX_LORA:
        checkpoint = _run_lora_training(validated)
        output = _run_voxcpm(validated, lora_checkpoint=checkpoint)
    else:
        output = _run_soulx(validated)
    outputs = [_output_record(output, "qualified_media_candidate")]
    if checkpoint is not None:
        outputs.append(_output_record(checkpoint / "lora_config.json", "lora_config"))
        weights = next(
            path
            for path in (
                checkpoint / "lora_weights.safetensors",
                checkpoint / "lora_weights.ckpt",
            )
            if path.is_file()
        )
        outputs.append(_output_record(weights, "lora_weights"))
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": validated["run_id"],
        "mode": validated["mode"],
        "manifest_sha256": validated["sha256"],
        "started_at": started_at,
        "completed_at": _iso_utc(),
        "status": "completed",
        "preflight": preflight,
        "input_binding": input_binding,
        "outputs": outputs,
    }
    record = _attach_run_attestation(record, attestation_key)
    _write_private_json(record_path, record)
    return record


def evaluate_metrics(mode: str, metrics_payload: dict[str, Any]) -> dict[str, Any]:
    if mode not in METRIC_PROFILES:
        raise WorkerPackError("unsupported metrics mode")
    if metrics_payload.get("schema_version") != SCHEMA_VERSION:
        raise WorkerPackError("unsupported metrics schema_version")
    metrics = metrics_payload.get("metrics")
    evaluators = metrics_payload.get("evaluators")
    if not isinstance(metrics, dict):
        raise WorkerPackError("metrics must be an object")
    if not isinstance(evaluators, list) or not evaluators:
        raise WorkerPackError("at least one pinned evaluator is required")
    for index, evaluator in enumerate(evaluators):
        if (
            not isinstance(evaluator, dict)
            or set(evaluator) != {"name", "version", "path", "artifact_sha256"}
            or not isinstance(evaluator.get("name"), str)
            or not evaluator["name"].strip()
            or len(evaluator["name"]) > 200
            or not isinstance(evaluator.get("version"), str)
            or not evaluator["version"].strip()
            or len(evaluator["version"]) > 200
        ):
            raise WorkerPackError(f"evaluators[{index}] is invalid")
        expected_digest = _validate_digest(
            evaluator.get("artifact_sha256"), f"evaluators[{index}].artifact_sha256"
        )
        evaluator_path = _assert_absolute_private_path(
            evaluator.get("path"), f"evaluators[{index}].path", must_exist=True
        )
        actual_digest, _ = _sha256_file(evaluator_path, maximum_bytes=2 * 1024**3)
        if actual_digest != expected_digest:
            raise WorkerPackError(f"evaluators[{index}] artifact hash does not match")
    checks: list[dict[str, Any]] = []
    for rule in METRIC_PROFILES[mode]:
        value = metrics.get(rule.name)
        if isinstance(rule.threshold, bool):
            passed = isinstance(value, bool) and value is rule.threshold
        elif (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            passed = False
        elif rule.operator == ">=":
            passed = float(value) >= float(rule.threshold)
        elif rule.operator == "<=":
            passed = float(value) <= float(rule.threshold)
        elif rule.operator == "==":
            passed = float(value) == float(rule.threshold)
        else:
            raise AssertionError(f"unsupported metric operator: {rule.operator}")
        checks.append(
            {
                "metric": rule.name,
                "operator": rule.operator,
                "threshold": rule.threshold,
                "value": value,
                "passed": passed,
            }
        )
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def _load_run_record(path: Path, validated: dict[str, Any]) -> dict[str, Any]:
    path = _assert_absolute_private_path(str(path), "record", must_exist=True)
    record = _read_json(path, maximum_bytes=16 * 1024 * 1024)
    required_fields = {
        "schema_version",
        "run_id",
        "mode",
        "manifest_sha256",
        "started_at",
        "completed_at",
        "status",
        "preflight",
        "input_binding",
        "outputs",
        "attestation",
    }
    if set(record) != required_fields:
        raise WorkerPackError("run record fields do not match the attested schema")
    _verify_run_attestation(record, _run_attestation_key())
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("run_id") != validated["run_id"]
        or record.get("mode") != validated["mode"]
        or record.get("manifest_sha256") != validated["sha256"]
        or record.get("status") != "completed"
    ):
        raise WorkerPackError("run record does not match the validated job")
    started_at = _parse_utc(record.get("started_at"), "run record started_at")
    completed_at = _parse_utc(record.get("completed_at"), "run record completed_at")
    if completed_at < started_at:
        raise WorkerPackError("run record timestamps are invalid")
    fresh_binding = _verify_validated_binding(
        validated, phase="provenance finalization"
    )
    recorded_binding = record.get("input_binding")
    if not isinstance(recorded_binding, dict):
        raise WorkerPackError("run record input binding is invalid")
    binding_fields = {
        "manifest_sha256",
        "artifacts",
        "models",
        "source_repositories",
    }
    if any(
        recorded_binding.get(field) != fresh_binding.get(field)
        for field in binding_fields
    ):
        raise WorkerPackError("run record input binding no longer matches the job")
    outputs = record.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise WorkerPackError("run record has no outputs")
    expected_kinds = ["qualified_media_candidate"]
    if validated["mode"] == MODE_VOX_LORA:
        expected_kinds.extend(("lora_config", "lora_weights"))
    if any(
        not isinstance(output, dict)
        or set(output) != {"kind", "path", "sha256", "size"}
        for output in outputs
    ):
        raise WorkerPackError("run record output entries are invalid")
    if sorted(output["kind"] for output in outputs) != sorted(expected_kinds):
        raise WorkerPackError("run record output kinds do not match the worker mode")
    candidate_path = (
        validated["execution"]["output_dir"] / validated["execution"]["output_filename"]
    )
    candidate_path = candidate_path.resolve(strict=True)
    checkpoint_root = validated["execution"].get("checkpoint_dir")
    checkpoint_real = (
        checkpoint_root.resolve(strict=True) if checkpoint_root is not None else None
    )
    for index, output in enumerate(outputs):
        if not isinstance(output, dict) or set(output) != {
            "kind",
            "path",
            "sha256",
            "size",
        }:
            raise WorkerPackError(f"run record output {index} is invalid")
        path_value = _assert_absolute_private_path(
            output.get("path"), f"run record output {index}", must_exist=True
        )
        path_value = path_value.resolve(strict=True)
        kind = output["kind"]
        if kind == "qualified_media_candidate" and path_value != candidate_path:
            raise WorkerPackError("run record candidate path is not the job output")
        if kind in {"lora_config", "lora_weights"}:
            if checkpoint_real is None or not _inside(path_value, checkpoint_real):
                raise WorkerPackError(
                    "run record LoRA output escapes the checkpoint root"
                )
            allowed_names = (
                {"lora_config.json"}
                if kind == "lora_config"
                else {"lora_weights.safetensors", "lora_weights.ckpt"}
            )
            if path_value.name not in allowed_names:
                raise WorkerPackError("run record LoRA output filename is invalid")
        expected_digest = _validate_digest(
            output.get("sha256"), f"run record output {index} digest"
        )
        actual_digest, actual_size = _sha256_file(path_value)
        expected_size = output.get("size")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or actual_digest != expected_digest
            or actual_size != expected_size
        ):
            raise WorkerPackError(f"run record output {index} changed after execution")
    return record


def finalize_provenance(
    validated: dict[str, Any],
    *,
    record_path: Path,
    metrics_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    record = _load_run_record(record_path, validated)
    metrics_path = _assert_absolute_private_path(
        str(metrics_path), "metrics", must_exist=True
    )
    metrics_payload = _read_json(metrics_path)
    _assert_no_embedded_secrets(metrics_payload)
    if metrics_payload.get("run_id") != validated["run_id"]:
        raise WorkerPackError("metrics run_id does not match the job")
    acceptance = evaluate_metrics(validated["mode"], metrics_payload)
    metrics_digest, _ = _sha256_file(metrics_path)
    preflight = record.get("preflight", {})
    outputs = [
        {
            "kind": output["kind"],
            "name": Path(output["path"]).name,
            "sha256": output["sha256"],
            "size": output["size"],
        }
        for output in record["outputs"]
    ]
    inputs = [
        {
            "id": artifact["id"],
            "role": artifact["role"],
            "sha256": artifact["sha256"],
            "size": artifact["size"],
            "media_type": artifact["media_type"],
        }
        for artifact in validated["artifacts"]
    ]
    provenance = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "echoweave_gpu_worker_provenance",
        "generated_at": _iso_utc(),
        "run_id": validated["run_id"],
        "mode": validated["mode"],
        "accepted": acceptance["passed"],
        "manifest_sha256": validated["sha256"],
        "authorization": validated["authorization"],
        "models": [
            {
                "id": model["id"],
                "revision": model["revision"],
                "snapshot_sha256": model["snapshot_sha256"],
            }
            for model in validated["models"]
        ],
        "source_repositories": [
            {"id": repo["id"], "revision": repo["revision"]}
            for repo in validated["repositories"]
        ],
        "runtime": {
            "gpu": preflight.get("gpu"),
            "versions": preflight.get("versions"),
            "platform": preflight.get("platform"),
        },
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "inputs": inputs,
        "outputs": outputs,
        "metrics_sha256": metrics_digest,
        "evaluators": [
            {
                "name": evaluator["name"],
                "version": evaluator["version"],
                "artifact_sha256": evaluator["artifact_sha256"],
            }
            for evaluator in metrics_payload["evaluators"]
        ],
        "acceptance": acceptance,
        "synthetic_media_disclosure_required": True,
    }
    output_path = _assert_absolute_private_path(
        str(output_path), "provenance output", must_exist=False
    )
    _write_private_json(output_path, provenance)
    return provenance


def _print_json(payload: object) -> None:
    sys.stdout.write(_json_bytes(payload).decode("utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="echoweave-gpu-worker",
        description="Validate and qualify pinned EchoWeave remote GPU jobs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser(
        "snapshot", help="hash a downloaded model snapshot"
    )
    snapshot.add_argument("--root", type=Path, required=True)
    snapshot.add_argument("--model-id", required=True)
    snapshot.add_argument("--revision", required=True)
    snapshot.add_argument("--output", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="validate a private job manifest")
    validate.add_argument("manifest", type=Path)
    validate.add_argument(
        "--metadata-only",
        action="store_true",
        help="validate structure without reading private/model files",
    )

    preflight = subparsers.add_parser(
        "preflight", help="run full job and GPU preflight"
    )
    preflight.add_argument("manifest", type=Path)
    preflight.add_argument("--output", type=Path)

    run = subparsers.add_parser("run", help="execute a fully validated private job")
    run.add_argument("manifest", type=Path)
    run.add_argument("--record", type=Path, required=True)

    finalize = subparsers.add_parser(
        "finalize", help="bind independent metrics and emit sanitized provenance"
    )
    finalize.add_argument("manifest", type=Path)
    finalize.add_argument("--record", type=Path, required=True)
    finalize.add_argument("--metrics", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            payload = create_model_snapshot(
                args.root,
                model_id=args.model_id,
                revision=args.revision,
                output=args.output,
            )
            _print_json(
                {
                    "ok": True,
                    "model_id": payload["model_id"],
                    "revision": payload["revision"],
                    "file_count": len(payload["files"]),
                }
            )
            return 0
        validated = validate_job(
            args.manifest,
            verify_files=not getattr(args, "metadata_only", False),
        )
        if args.command == "validate":
            _print_json(
                {
                    "ok": True,
                    "run_id": validated["run_id"],
                    "mode": validated["mode"],
                    "manifest_sha256": validated["sha256"],
                }
            )
            return 0
        if args.command == "preflight":
            payload = preflight_job(validated)
            if args.output:
                output = _assert_absolute_private_path(
                    str(args.output), "preflight output", must_exist=False
                )
                _write_private_json(output, payload)
            _print_json(payload)
            return 0 if payload["ok"] else 2
        if args.command == "run":
            payload = run_job(validated, args.record)
            _print_json(
                {"ok": True, "run_id": payload["run_id"], "status": payload["status"]}
            )
            return 0
        payload = finalize_provenance(
            validated,
            record_path=args.record,
            metrics_path=args.metrics,
            output_path=args.output,
        )
        _print_json(
            {"ok": True, "run_id": payload["run_id"], "accepted": payload["accepted"]}
        )
        return 0 if payload["accepted"] else 2
    except WorkerPackError as exc:
        sys.stderr.write(f"gpu-worker error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
