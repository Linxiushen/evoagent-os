"""Export a provenance-bound VoxCPM2 dataset plan from completed reviews."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import wave
from collections.abc import Iterable
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = 1
SAMPLE_RATE = 16_000
MAX_REVIEW_INPUT_BYTES = 64 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
LOCAL_AUTHORIZATION_STATES = {
    "operator-verified",
    "user-attested-local-collection-and-training",
}
EXPECTED_INPUT_ROLES = {
    "qwen_ranges",
    "burned_subtitle_ocr",
    "source_audio",
    "authorization_record",
}


class ReviewExportError(ValueError):
    """Raised when review evidence cannot safely become a dataset plan."""


@lru_cache(maxsize=1)
def _review_module() -> ModuleType:
    name = "_echoweave_voxcpm_review_contract"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().with_name("review_voxcpm_queue.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReviewExportError("could not load the review queue contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _strict_number(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise ReviewExportError(f"{label} must be a finite JSON number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ReviewExportError(f"{label} must be a finite JSON number") from exc
    if not math.isfinite(number):
        raise ReviewExportError(f"{label} must be a finite JSON number")
    return number


def _strict_clip_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not CLIP_ID.fullmatch(value):
        raise ReviewExportError(f"{label} is invalid")
    return value


def _strict_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ReviewExportError(f"{label} must be a lowercase SHA-256")
    return value


def _strict_size(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ReviewExportError(f"{label} must be a positive integer")
    return value


def _require_exact_keys(
    value: Any, *, label: str, expected: set[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewExportError(f"{label} must be an object")
    if set(value) != expected:
        missing = expected - value.keys()
        extra = value.keys() - expected
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unsupported " + ", ".join(sorted(extra)))
        raise ReviewExportError(f"{label} fields are invalid: {'; '.join(details)}")
    return value


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_file(path: Path, label: str) -> Path:
    review = _review_module()
    try:
        return review._safe_file(path, label)
    except review.ReviewToolError as exc:
        raise ReviewExportError(str(exc)) from exc


def _read_review_json(
    path: Path, label: str, maximum_bytes: int
) -> tuple[dict[str, Any], str, int]:
    review = _review_module()
    try:
        return review._read_json(path, label, maximum_bytes)
    except review.ReviewToolError as exc:
        raise ReviewExportError(str(exc)) from exc


def validate_local_authorization_record(
    *,
    path: Path,
    sha256: str,
    size_bytes: int,
    expected_status: str,
) -> dict[str, Any]:
    if expected_status not in LOCAL_AUTHORIZATION_STATES:
        raise ReviewExportError("authorization status does not permit local training")
    payload, digest, size = _read_review_json(
        path, "authorization record", MAX_REVIEW_INPUT_BYTES
    )
    if digest != sha256 or size != size_bytes:
        raise ReviewExportError("authorization record binding does not match")
    status = payload.get("status")
    if not isinstance(status, str) or status not in LOCAL_AUTHORIZATION_STATES:
        raise ReviewExportError(
            "authorization record status does not permit local training"
        )
    if status != expected_status:
        raise ReviewExportError(
            "authorization record status does not match the requested local status"
        )
    return {
        "path": _safe_file(path, "authorization record"),
        "sha256": digest,
        "size_bytes": size,
        "status": status,
    }


def _verify_current_file(
    path: Path, *, sha256: str, size_bytes: int, label: str
) -> None:
    current = _safe_file(path, label)
    digest, size = _file_digest(current)
    if digest != sha256 or size != size_bytes:
        raise ReviewExportError(f"{label} changed during review export")


def _resolve_binding_path(raw: Any, *, base: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ReviewExportError(f"{label}.path must be non-empty text")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    return _safe_file(candidate, label)


def _validate_public_binding(
    raw: Any,
    *,
    label: str,
    base: Path,
    expected_role: str | None = None,
) -> dict[str, Any]:
    expected = {"path", "sha256", "size_bytes"}
    if expected_role is not None:
        expected.add("role")
    value = _require_exact_keys(raw, label=label, expected=expected)
    if expected_role is not None and value["role"] != expected_role:
        raise ReviewExportError(f"{label}.role does not match {expected_role}")
    path = _resolve_binding_path(value["path"], base=base, label=label)
    digest = _strict_digest(value["sha256"], f"{label}.sha256")
    size = _strict_size(value["size_bytes"], f"{label}.size_bytes")
    actual_digest, actual_size = _file_digest(path)
    if actual_digest != digest or actual_size != size:
        raise ReviewExportError(f"{label} binding does not match its file")
    return {"path": path, "sha256": digest, "size_bytes": size}


def _source_wav_metadata(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise ReviewExportError("queue source audio must be a PCM WAV file") from exc
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != SAMPLE_RATE
        or compression != "NONE"
        or frames <= 0
    ):
        raise ReviewExportError("queue source audio must be mono PCM16 16kHz WAV")
    return {
        "container": "wav",
        "codec": "pcm_s16le",
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration_seconds": frames / sample_rate,
    }


def _validate_queue_source_metadata(raw: Any, actual: dict[str, Any]) -> None:
    expected_keys = set(actual)
    value = _require_exact_keys(
        raw, label="review queue.source_audio", expected=expected_keys
    )
    for field in expected_keys - {"duration_seconds"}:
        if (
            type(value[field]) is not type(actual[field])
            or value[field] != actual[field]
        ):
            raise ReviewExportError(
                f"review queue.source_audio.{field} does not match the source WAV"
            )
    duration = _strict_number(
        value["duration_seconds"], "review queue.source_audio.duration_seconds"
    )
    if abs(duration - actual["duration_seconds"]) > 1 / SAMPLE_RATE:
        raise ReviewExportError(
            "review queue.source_audio.duration_seconds does not match the source WAV"
        )


def _queue_inputs(payload: dict[str, Any], queue_root: Path) -> dict[str, Any]:
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list):
        raise ReviewExportError("review queue.inputs must be an array")
    by_role: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_inputs):
        if not isinstance(raw, dict):
            raise ReviewExportError(f"review queue.inputs[{index}] must be an object")
        role = raw.get("role")
        if not isinstance(role, str) or role not in EXPECTED_INPUT_ROLES:
            raise ReviewExportError(f"review queue.inputs[{index}].role is invalid")
        if role in by_role:
            raise ReviewExportError(f"duplicate review queue input role: {role}")
        by_role[role] = _validate_public_binding(
            raw,
            label=f"review queue.inputs[{index}]",
            base=queue_root,
            expected_role=role,
        )
    if set(by_role) != EXPECTED_INPUT_ROLES:
        missing = EXPECTED_INPUT_ROLES - by_role.keys()
        raise ReviewExportError(
            "review queue is missing input roles: " + ", ".join(sorted(missing))
        )
    return by_role


def _validate_queue_clips(
    payload: dict[str, Any], queue: Any, source: dict[str, Any]
) -> list[dict[str, Any]]:
    raw_clips = payload.get("clips")
    if not isinstance(raw_clips, list) or len(raw_clips) != len(queue.clips):
        raise ReviewExportError("review queue clips changed during validation")
    result: list[dict[str, Any]] = []
    for index, (raw, checked) in enumerate(zip(raw_clips, queue.clips, strict=True)):
        if not isinstance(raw, dict) or raw.get("clip_id") != checked.clip_id:
            raise ReviewExportError(
                f"review queue clips[{index}] order is inconsistent"
            )
        start = _strict_number(
            raw.get("start_seconds"), f"review queue clips[{index}].start_seconds"
        )
        end = _strict_number(
            raw.get("end_seconds"), f"review queue clips[{index}].end_seconds"
        )
        if (
            start < 0
            or end <= start
            or end > source["duration_seconds"] + 1 / SAMPLE_RATE
        ):
            raise ReviewExportError(f"review queue clips[{index}] range is invalid")
        if abs((end - start) - checked.duration_seconds) > 0.02:
            raise ReviewExportError(
                f"review queue clips[{index}] range does not match reviewed WAV duration"
            )
        result.append(
            {
                "clip_id": checked.clip_id,
                "start_seconds": start,
                "end_seconds": end,
                "review_audio_path": checked.audio_path,
                "review_audio_sha256": checked.audio_sha256,
                "review_audio_size_bytes": checked.audio_size_bytes,
            }
        )
    return result


def _load_review_pair(queue_path: Path, decisions_path: Path) -> dict[str, Any]:
    review = _review_module()
    try:
        queue = review.QueueSnapshot.load(queue_path)
        payload, queue_digest, queue_size = review._read_json(
            queue.manifest_path,
            "review queue manifest",
            review.MAX_MANIFEST_BYTES,
        )
        if queue_digest != queue.sha256 or queue_size != queue.size_bytes:
            raise ReviewExportError("review queue changed during validation")
        inputs = _queue_inputs(payload, queue.root)
        source_metadata = _source_wav_metadata(inputs["source_audio"]["path"])
        source = {**inputs["source_audio"], **source_metadata}
        _validate_queue_source_metadata(payload.get("source_audio"), source_metadata)
        queue_clips = _validate_queue_clips(payload, queue, source)

        store = review.DecisionStore(decisions_path, queue)
        decisions = store.load()
        decision_payload, decision_digest, decision_size = review._read_json(
            store.path, "decisions file", review.MAX_DECISIONS_BYTES
        )
    except review.ReviewToolError as exc:
        raise ReviewExportError(str(exc)) from exc

    queue_binding = decision_payload.get("queue")
    if not isinstance(queue_binding, dict):
        raise ReviewExportError("decisions queue binding is missing")
    raw_queue_path = queue_binding.get("path")
    if not isinstance(raw_queue_path, str) or "\x00" in raw_queue_path:
        raise ReviewExportError("decisions queue path binding is invalid")
    if Path(raw_queue_path).resolve() != queue.manifest_path:
        raise ReviewExportError("decisions file path is bound to another queue")

    expected_ids = [clip.clip_id for clip in queue.clips]
    if set(decisions) != set(expected_ids):
        missing = set(expected_ids) - decisions.keys()
        raise ReviewExportError(
            "every queue clip requires a stored decision; missing: "
            + ", ".join(sorted(missing))
        )
    checked_clips: list[dict[str, Any]] = []
    decision_clips: list[dict[str, Any]] = []
    by_id = {item["clip_id"]: item for item in queue_clips}
    for clip_id in expected_ids:
        decision = decisions[clip_id]
        updated_at = decision.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ReviewExportError(f"decision updated_at is missing for {clip_id}")
        approved = decision.get("gates_satisfied") is True
        if decision.get("approved") is not approved:
            raise ReviewExportError(f"stored approval is inconsistent for {clip_id}")
        decision_clips.append(
            {
                "clip_id": clip_id,
                "decision_updated_at": updated_at,
                "approved": approved,
            }
        )
        if not approved:
            continue
        corrected = decision.get("corrected_text")
        if (
            not isinstance(corrected, str)
            or not corrected.strip()
            or "\x00" in corrected
        ):
            raise ReviewExportError(f"corrected_text is invalid for {clip_id}")
        checked_clips.append(
            {
                **by_id[clip_id],
                "text": corrected.strip(),
                "decision_updated_at": updated_at,
                "review": {
                    "approved_for_training": True,
                    "target_speaker_only": True,
                    "transcript_verified": True,
                    "third_party_speech": False,
                    "background_music": False,
                },
            }
        )

    queue.verify_manifest()
    for clip in queue.clips:
        queue.verify_audio(clip)
    for role, binding in inputs.items():
        _verify_current_file(
            binding["path"],
            sha256=binding["sha256"],
            size_bytes=binding["size_bytes"],
            label=f"review queue input {role}",
        )
    _verify_current_file(
        store.path,
        sha256=decision_digest,
        size_bytes=decision_size,
        label="decisions file",
    )
    return {
        "queue": {
            "path": queue.manifest_path,
            "sha256": queue.sha256,
            "size_bytes": queue.size_bytes,
        },
        "decisions": {
            "path": store.path,
            "sha256": decision_digest,
            "size_bytes": decision_size,
        },
        "authorization": inputs["authorization_record"],
        "source": source,
        "decision_clips": decision_clips,
        "approved_clips": checked_clips,
    }


def _evidence_binding(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(binding["path"]),
        "sha256": binding["sha256"],
        "size_bytes": binding["size_bytes"],
    }


def _validate_output_path(output_path: Path) -> Path:
    review = _review_module()
    candidate = Path(os.path.abspath(os.fspath(output_path)))
    try:
        review._reject_link_components(candidate, "output path")
    except review.ReviewToolError as exc:
        raise ReviewExportError(str(exc)) from exc
    if candidate.exists():
        raise ReviewExportError(f"output already exists: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    try:
        review._reject_link_components(candidate.parent, "output parent")
    except review.ReviewToolError as exc:
        raise ReviewExportError(str(exc)) from exc
    if not candidate.parent.is_dir():
        raise ReviewExportError("output parent must be a directory")
    return candidate


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _write_new_file(output: Path, payload: dict[str, Any]) -> None:
    data = _json_bytes(payload)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    created_output = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with output.open("xb") as destination, temporary.open("rb") as source:
            created_output = True
            destination.write(source.read())
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        if created_output:
            output.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def export_dataset_plan(
    *,
    review_pairs: Iterable[tuple[Path, Path]],
    authorization_path: Path,
    authorization_status: str,
    dataset_id: str,
    subject_id: str,
    output_path: Path,
    validation_clip_ids: Iterable[str] = (),
) -> Path:
    for label, value in (("dataset_id", dataset_id), ("subject_id", subject_id)):
        if not isinstance(value, str) or not CLIP_ID.fullmatch(value):
            raise ReviewExportError(f"{label} is invalid")
    output = _validate_output_path(output_path)
    authorization = _safe_file(authorization_path, "authorization record")
    authorization_digest, authorization_size = _file_digest(authorization)
    authorization_record = validate_local_authorization_record(
        path=authorization,
        sha256=authorization_digest,
        size_bytes=authorization_size,
        expected_status=authorization_status,
    )

    pairs = list(review_pairs)
    if not pairs:
        raise ReviewExportError("at least one review pair is required")
    snapshots = [_load_review_pair(queue, decisions) for queue, decisions in pairs]
    validation_ids = [
        _strict_clip_id(value, f"validation_clip_ids[{index}]")
        for index, value in enumerate(validation_clip_ids)
    ]
    if len(set(validation_ids)) != len(validation_ids):
        raise ReviewExportError("validation clip IDs must be unique")
    validation = set(validation_ids)

    sources: list[dict[str, Any]] = []
    source_ids: dict[tuple[Path, str, int], str] = {}
    evidence: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    seen_review_ids: set[str] = set()
    seen_review_names: dict[str, str] = {}
    seen_pairs: set[tuple[Path, Path]] = set()
    for snapshot in snapshots:
        pair_key = (snapshot["queue"]["path"], snapshot["decisions"]["path"])
        if pair_key in seen_pairs:
            raise ReviewExportError("duplicate queue/decisions review pair")
        seen_pairs.add(pair_key)
        queue_authorization = snapshot["authorization"]
        if (
            queue_authorization["path"] != authorization
            or queue_authorization["sha256"] != authorization_digest
            or queue_authorization["size_bytes"] != authorization_size
        ):
            raise ReviewExportError(
                "all queues must bind the same supplied authorization record"
            )

        source = snapshot["source"]
        source_key = (source["path"], source["sha256"], source["size_bytes"])
        source_id = source_ids.get(source_key)
        if source_id is None:
            source_id = f"source-{len(source_ids) + 1:03d}"
            source_ids[source_key] = source_id
            sources.append(
                {
                    "source_id": source_id,
                    "audio_path": str(source["path"]),
                    "audio_sha256": source["sha256"],
                }
            )

        for decision_clip in snapshot["decision_clips"]:
            reviewed_id = decision_clip["clip_id"]
            if reviewed_id in seen_review_ids:
                raise ReviewExportError(f"duplicate global clip_id: {reviewed_id}")
            seen_review_ids.add(reviewed_id)
            reviewed_name = f"{reviewed_id}.wav".casefold()
            if reviewed_name in seen_review_names:
                raise ReviewExportError(
                    "case-insensitive global clip filename collision: "
                    f"{seen_review_names[reviewed_name]} and {reviewed_id}"
                )
            seen_review_names[reviewed_name] = reviewed_id

        evidence_clips = []
        for clip in snapshot["approved_clips"]:
            clip_id = clip["clip_id"]
            if clip_id in seen_ids:
                raise ReviewExportError(f"duplicate global clip_id: {clip_id}")
            seen_ids.add(clip_id)
            name = f"{clip_id}.wav".casefold()
            if name in seen_names:
                raise ReviewExportError(
                    "case-insensitive global clip filename collision: "
                    f"{seen_names[name]} and {clip_id}"
                )
            seen_names[name] = clip_id
            clips.append(
                {
                    "clip_id": clip_id,
                    "source_id": source_id,
                    "start_seconds": clip["start_seconds"],
                    "end_seconds": clip["end_seconds"],
                    "text": clip["text"],
                    "split": "validation" if clip_id in validation else "train",
                    "review": clip["review"],
                }
            )
            evidence_clips.append(
                {
                    "clip_id": clip_id,
                    "decision_updated_at": clip["decision_updated_at"],
                }
            )
        evidence.append(
            {
                "source_id": source_id,
                "queue": _evidence_binding(snapshot["queue"]),
                "decisions": _evidence_binding(snapshot["decisions"]),
                "approved_clips": evidence_clips,
            }
        )

    unknown_validation = validation - seen_ids
    if unknown_validation:
        raise ReviewExportError(
            "unknown validation clip IDs: " + ", ".join(sorted(unknown_validation))
        )
    if not clips:
        raise ReviewExportError("at least one approved clip is required")
    if all(clip["split"] == "validation" for clip in clips):
        raise ReviewExportError("at least one clip must remain in the train split")
    for source_id in source_ids.values():
        ranges = sorted(
            (clip["start_seconds"], clip["end_seconds"], clip["clip_id"])
            for clip in clips
            if clip["source_id"] == source_id
        )
        for previous, current in pairwise(ranges):
            if current[0] < previous[1]:
                raise ReviewExportError(
                    f"overlapping reviewed clips: {previous[2]} and {current[2]}"
                )

    plan = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "subject_id": subject_id,
        "authorization": {
            "record_path": str(authorization),
            "record_sha256": authorization_digest,
            "status": authorization_status,
            "third_party_model_processing": False,
        },
        "sources": sources,
        "review_evidence": evidence,
        "clips": clips,
    }
    for snapshot in snapshots:
        _verify_current_file(
            snapshot["queue"]["path"],
            sha256=snapshot["queue"]["sha256"],
            size_bytes=snapshot["queue"]["size_bytes"],
            label="review queue manifest",
        )
        _verify_current_file(
            snapshot["decisions"]["path"],
            sha256=snapshot["decisions"]["sha256"],
            size_bytes=snapshot["decisions"]["size_bytes"],
            label="decisions file",
        )
    validate_local_authorization_record(
        path=authorization_record["path"],
        sha256=authorization_record["sha256"],
        size_bytes=authorization_record["size_bytes"],
        expected_status=authorization_status,
    )
    _write_new_file(output, plan)
    return output


def validate_plan_review_evidence(
    *,
    raw_evidence: Any,
    base: Path,
    authorization: dict[str, Any],
    authorization_status: str,
    sources: dict[str, dict[str, Any]],
    clips: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ReviewExportError("review_evidence must be a non-empty array")
    plan_clips = {clip["clip_id"]: clip for clip in clips}
    validate_local_authorization_record(
        path=authorization["path"],
        sha256=authorization["sha256"],
        size_bytes=authorization["size_bytes"],
        expected_status=authorization_status,
    )
    seen_clips: set[str] = set()
    seen_names: dict[str, str] = {}
    seen_review_ids: set[str] = set()
    seen_review_names: dict[str, str] = {}
    seen_pairs: set[tuple[Path, Path]] = set()
    verified: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_evidence):
        item = _require_exact_keys(
            raw,
            label=f"review_evidence[{index}]",
            expected={"source_id", "queue", "decisions", "approved_clips"},
        )
        source_id = item["source_id"]
        if not isinstance(source_id, str) or source_id not in sources:
            raise ReviewExportError(f"review_evidence[{index}].source_id is unknown")
        queue_binding = _validate_public_binding(
            item["queue"],
            label=f"review_evidence[{index}].queue",
            base=base,
        )
        decision_binding = _validate_public_binding(
            item["decisions"],
            label=f"review_evidence[{index}].decisions",
            base=base,
        )
        pair = (queue_binding["path"], decision_binding["path"])
        if pair in seen_pairs:
            raise ReviewExportError("review_evidence contains a duplicate review pair")
        seen_pairs.add(pair)
        snapshot = _load_review_pair(*pair)
        for label, expected, actual in (
            ("queue", queue_binding, snapshot["queue"]),
            ("decisions", decision_binding, snapshot["decisions"]),
        ):
            if any(
                actual[field] != expected[field]
                for field in ("path", "sha256", "size_bytes")
            ):
                raise ReviewExportError(
                    f"review_evidence[{index}].{label} binding changed"
                )
        if any(
            snapshot["authorization"][field] != authorization[field]
            for field in ("path", "sha256", "size_bytes")
        ):
            raise ReviewExportError(
                f"review_evidence[{index}] authorization does not match the plan"
            )
        source = sources[source_id]
        if any(
            snapshot["source"][field] != source[field]
            for field in ("path", "sha256", "size_bytes")
        ):
            raise ReviewExportError(
                f"review_evidence[{index}] source does not match {source_id}"
            )

        for decision_clip in snapshot["decision_clips"]:
            reviewed_id = decision_clip["clip_id"]
            if reviewed_id in seen_review_ids:
                raise ReviewExportError(
                    f"duplicate global reviewed clip_id: {reviewed_id}"
                )
            seen_review_ids.add(reviewed_id)
            reviewed_name = f"{reviewed_id}.wav".casefold()
            if reviewed_name in seen_review_names:
                raise ReviewExportError(
                    "case-insensitive reviewed clip collision: "
                    f"{seen_review_names[reviewed_name]} and {reviewed_id}"
                )
            seen_review_names[reviewed_name] = reviewed_id

        raw_clip_bindings = item["approved_clips"]
        if not isinstance(raw_clip_bindings, list):
            raise ReviewExportError(
                f"review_evidence[{index}].approved_clips is invalid"
            )
        if len(raw_clip_bindings) != len(snapshot["approved_clips"]):
            raise ReviewExportError(f"review_evidence[{index}].approved_clips changed")
        checked_evidence_clips = []
        for clip_index, (raw_clip, reviewed) in enumerate(
            zip(raw_clip_bindings, snapshot["approved_clips"], strict=True)
        ):
            binding = _require_exact_keys(
                raw_clip,
                label=f"review_evidence[{index}].approved_clips[{clip_index}]",
                expected={"clip_id", "decision_updated_at"},
            )
            clip_id = _strict_clip_id(
                binding["clip_id"],
                f"review_evidence[{index}].approved_clips[{clip_index}].clip_id",
            )
            if clip_id != reviewed["clip_id"]:
                raise ReviewExportError(
                    f"review_evidence[{index}].approved_clips[{clip_index}] mapping changed"
                )
            if binding["decision_updated_at"] != reviewed["decision_updated_at"]:
                raise ReviewExportError(
                    f"review decision timestamp changed for {clip_id}"
                )
            if clip_id in seen_clips:
                raise ReviewExportError(f"duplicate global reviewed clip_id: {clip_id}")
            seen_clips.add(clip_id)
            filename = f"{clip_id}.wav".casefold()
            if filename in seen_names:
                raise ReviewExportError(
                    "case-insensitive reviewed clip collision: "
                    f"{seen_names[filename]} and {clip_id}"
                )
            seen_names[filename] = clip_id
            plan_clip = plan_clips.get(clip_id)
            if plan_clip is None:
                raise ReviewExportError(
                    f"reviewed clip is missing from plan: {clip_id}"
                )
            if plan_clip["source_id"] != source_id:
                raise ReviewExportError(f"plan source_id changed for {clip_id}")
            if (
                abs(plan_clip["start_seconds"] - reviewed["start_seconds"]) > 1e-9
                or abs(plan_clip["end_seconds"] - reviewed["end_seconds"]) > 1e-9
            ):
                raise ReviewExportError(f"plan timestamps changed for {clip_id}")
            if plan_clip["text"] != reviewed["text"]:
                raise ReviewExportError(f"plan corrected text changed for {clip_id}")
            if plan_clip["review"] != reviewed["review"]:
                raise ReviewExportError(f"plan review gates changed for {clip_id}")
            checked_evidence_clips.append(
                {
                    "clip_id": clip_id,
                    "decision_updated_at": reviewed["decision_updated_at"],
                    "review_audio_path": str(reviewed["review_audio_path"]),
                    "review_audio_sha256": reviewed["review_audio_sha256"],
                    "review_audio_size_bytes": reviewed["review_audio_size_bytes"],
                }
            )
        verified.append(
            {
                "source_id": source_id,
                "queue": _evidence_binding(snapshot["queue"]),
                "decisions": _evidence_binding(snapshot["decisions"]),
                "approved_clips": checked_evidence_clips,
            }
        )
    if seen_clips != set(plan_clips):
        unreviewed = set(plan_clips) - seen_clips
        raise ReviewExportError(
            "plan contains clips without review evidence: "
            + ", ".join(sorted(unreviewed))
        )
    return verified


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-pair",
        action="append",
        nargs=2,
        required=True,
        metavar=("QUEUE", "DECISIONS"),
        help="immutable review-queue.json and its external decisions.json",
    )
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument(
        "--authorization-status",
        choices=sorted(LOCAL_AUTHORIZATION_STATES),
        required=True,
    )
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--validation-clip-id", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        output = export_dataset_plan(
            review_pairs=[(Path(pair[0]), Path(pair[1])) for pair in args.review_pair],
            authorization_path=args.authorization,
            authorization_status=args.authorization_status,
            dataset_id=args.dataset_id,
            subject_id=args.subject_id,
            output_path=args.output,
            validation_clip_ids=args.validation_clip_id,
        )
    except (OSError, ReviewExportError) as exc:
        raise SystemExit(f"dataset plan export failed: {exc}") from exc
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
