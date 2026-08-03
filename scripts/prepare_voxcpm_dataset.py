"""Build a provenance-bound VoxCPM2 JSONL dataset from reviewed audio ranges."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = 1
SAMPLE_RATE = 16_000
MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 30.0
MIN_TRAIN_SECONDS = 300.0
MAX_TRAIN_SECONDS = 600.0
CLIP_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
LOCAL_AUTHORIZATION_STATES = {
    "operator-verified",
    "user-attested-local-collection-and-training",
}


class DatasetPlanError(ValueError):
    """Raised when a dataset plan is incomplete or unsafe to execute."""


@lru_cache(maxsize=1)
def _review_export_module() -> ModuleType:
    name = "_echoweave_voxcpm_review_export"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().with_name("export_voxcpm_dataset_plan.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise DatasetPlanError("could not load the review evidence validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetPlanError(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: dict[str, Any],
    *,
    label: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise DatasetPlanError(f"{label} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise DatasetPlanError(
            f"{label} has unknown fields: {', '.join(sorted(unknown))}"
        )


def _resolve_bound_file(
    raw_path: Any,
    expected_sha256: Any,
    *,
    base: Path,
    label: str,
) -> tuple[Path, str, int]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise DatasetPlanError(f"{label}.path must be a non-empty string")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise DatasetPlanError(f"{label}.sha256 must be a lowercase SHA-256")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_symlink():
        raise DatasetPlanError(f"{label}.path must not be a symbolic link")
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DatasetPlanError(f"{label}.path could not be resolved") from exc
    if not path.is_file():
        raise DatasetPlanError(f"{label}.path must resolve to a regular file")
    actual_sha256, size = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise DatasetPlanError(f"{label}.sha256 does not match {path}")
    return path, actual_sha256, size


def load_and_validate_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.resolve(strict=True)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetPlanError(f"could not read dataset plan: {exc}") from exc
    plan = _require_mapping(plan, "plan")
    _require_exact_keys(
        plan,
        label="plan",
        required={
            "schema_version",
            "dataset_id",
            "subject_id",
            "authorization",
            "sources",
            "review_evidence",
            "clips",
        },
    )
    if (
        type(plan["schema_version"]) is not int
        or plan["schema_version"] != SCHEMA_VERSION
    ):
        raise DatasetPlanError(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("dataset_id", "subject_id"):
        if not isinstance(plan[field], str) or not CLIP_ID.fullmatch(plan[field]):
            raise DatasetPlanError(f"{field} contains unsupported characters")

    base = plan_path.parent
    authorization = _require_mapping(plan["authorization"], "authorization")
    _require_exact_keys(
        authorization,
        label="authorization",
        required={"record_path", "record_sha256", "status"},
        optional={"third_party_model_processing"},
    )
    if (
        not isinstance(authorization["status"], str)
        or authorization["status"] not in LOCAL_AUTHORIZATION_STATES
    ):
        raise DatasetPlanError("authorization status does not permit local preparation")
    third_party_processing = authorization.get("third_party_model_processing", False)
    if type(third_party_processing) is not bool:
        raise DatasetPlanError(
            "authorization.third_party_model_processing must be a boolean"
        )
    if third_party_processing is not False:
        raise DatasetPlanError(
            "authorization.third_party_model_processing must be false for local training"
        )
    record_path, record_sha256, record_size = _resolve_bound_file(
        authorization["record_path"],
        authorization["record_sha256"],
        base=base,
        label="authorization",
    )
    authorization["_resolved"] = {
        "path": record_path,
        "sha256": record_sha256,
        "size_bytes": record_size,
    }

    sources = plan["sources"]
    if not isinstance(sources, list) or not sources:
        raise DatasetPlanError("sources must be a non-empty array")
    resolved_sources: dict[str, dict[str, Any]] = {}
    for index, raw_source in enumerate(sources):
        source = _require_mapping(raw_source, f"sources[{index}]")
        _require_exact_keys(
            source,
            label=f"sources[{index}]",
            required={"source_id", "audio_path", "audio_sha256"},
        )
        source_id = source["source_id"]
        if not isinstance(source_id, str) or not CLIP_ID.fullmatch(source_id):
            raise DatasetPlanError(f"sources[{index}].source_id is invalid")
        if source_id in resolved_sources:
            raise DatasetPlanError(f"duplicate source_id: {source_id}")
        path, digest, size = _resolve_bound_file(
            source["audio_path"],
            source["audio_sha256"],
            base=base,
            label=f"sources[{index}]",
        )
        resolved_sources[source_id] = {
            "source_id": source_id,
            "path": path,
            "sha256": digest,
            "size_bytes": size,
        }

    clips = plan["clips"]
    if not isinstance(clips, list) or not clips:
        raise DatasetPlanError("clips must be a non-empty array")
    seen_ids: set[str] = set()
    seen_output_names: dict[str, str] = {}
    resolved_clips: list[dict[str, Any]] = []
    for index, raw_clip in enumerate(clips):
        clip = _require_mapping(raw_clip, f"clips[{index}]")
        _require_exact_keys(
            clip,
            label=f"clips[{index}]",
            required={
                "clip_id",
                "source_id",
                "start_seconds",
                "end_seconds",
                "text",
                "split",
                "review",
            },
        )
        clip_id = clip["clip_id"]
        if not isinstance(clip_id, str) or not CLIP_ID.fullmatch(clip_id):
            raise DatasetPlanError(f"clips[{index}].clip_id is invalid")
        if clip_id in seen_ids:
            raise DatasetPlanError(f"duplicate clip_id: {clip_id}")
        seen_ids.add(clip_id)
        output_name = f"{clip_id}.wav".casefold()
        if output_name in seen_output_names:
            raise DatasetPlanError(
                "clip_id output filename collision: "
                f"{seen_output_names[output_name]} and {clip_id}"
            )
        seen_output_names[output_name] = clip_id
        source_id = clip["source_id"]
        if not isinstance(source_id, str) or not CLIP_ID.fullmatch(source_id):
            raise DatasetPlanError(f"clips[{index}].source_id is invalid")
        if source_id not in resolved_sources:
            raise DatasetPlanError(f"clips[{index}] references an unknown source")
        if not isinstance(clip["split"], str) or clip["split"] not in {
            "train",
            "validation",
        }:
            raise DatasetPlanError(f"clips[{index}].split must be train or validation")
        if not isinstance(clip["text"], str) or not clip["text"].strip():
            raise DatasetPlanError(f"clips[{index}].text must be non-empty")
        if "\x00" in clip["text"]:
            raise DatasetPlanError(f"clips[{index}].text contains NUL")
        raw_start = clip["start_seconds"]
        raw_end = clip["end_seconds"]
        if type(raw_start) not in {int, float} or type(raw_end) not in {int, float}:
            raise DatasetPlanError(f"clips[{index}] has invalid timestamps")
        try:
            start = float(raw_start)
            end = float(raw_end)
        except OverflowError as exc:
            raise DatasetPlanError(f"clips[{index}] has invalid timestamps") from exc
        duration = end - start
        if not all(math.isfinite(value) for value in (start, end)) or start < 0:
            raise DatasetPlanError(f"clips[{index}] has invalid timestamps")
        if not MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS:
            raise DatasetPlanError(
                f"clips[{index}] duration must be {MIN_CLIP_SECONDS}-{MAX_CLIP_SECONDS}s"
            )
        review = _require_mapping(clip["review"], f"clips[{index}].review")
        _require_exact_keys(
            review,
            label=f"clips[{index}].review",
            required={
                "approved_for_training",
                "target_speaker_only",
                "transcript_verified",
                "third_party_speech",
                "background_music",
            },
            optional={"notes"},
        )
        expected_review = {
            "approved_for_training": True,
            "target_speaker_only": True,
            "transcript_verified": True,
            "third_party_speech": False,
            "background_music": False,
        }
        for field, expected in expected_review.items():
            if review[field] is not expected:
                raise DatasetPlanError(
                    f"clips[{index}].review.{field} must be {str(expected).lower()}"
                )
        resolved_clips.append(
            {
                **clip,
                "start_seconds": start,
                "end_seconds": end,
                "duration_seconds": duration,
            }
        )

    for source_id in resolved_sources:
        ranges = sorted(
            (
                clip["start_seconds"],
                clip["end_seconds"],
                clip["clip_id"],
            )
            for clip in resolved_clips
            if clip["source_id"] == source_id
        )
        for previous, current in pairwise(ranges):
            if current[0] < previous[1]:
                raise DatasetPlanError(
                    f"overlapping clips for {source_id}: {previous[2]} and {current[2]}"
                )

    review_export = _review_export_module()
    try:
        resolved_review_evidence = review_export.validate_plan_review_evidence(
            raw_evidence=plan["review_evidence"],
            base=base,
            authorization=authorization["_resolved"],
            authorization_status=authorization["status"],
            sources=resolved_sources,
            clips=resolved_clips,
        )
    except review_export.ReviewExportError as exc:
        raise DatasetPlanError(f"review evidence validation failed: {exc}") from exc

    plan["_resolved"] = {
        "plan_path": plan_path,
        "authorization": authorization.pop("_resolved"),
        "sources": resolved_sources,
        "clips": resolved_clips,
        "review_evidence": resolved_review_evidence,
    }
    return plan


def _ffmpeg_version(ffmpeg: Path) -> str:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise DatasetPlanError("ffmpeg did not return a version")
    return result.stdout.splitlines()[0].strip()


def _extract_clip(
    ffmpeg: Path, source: Path, destination: Path, start: float, end: float
) -> list[str]:
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(destination),
    ]
    result = subprocess.run(command, check=False, capture_output=True, timeout=120)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise DatasetPlanError(f"ffmpeg failed for {destination.name}: {detail}")
    return command


def _wav_metadata(path: Path, expected_duration: float) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise DatasetPlanError(f"invalid WAV output: {path}") from exc
    if channels != 1 or sample_width != 2 or sample_rate != SAMPLE_RATE or frames <= 0:
        raise DatasetPlanError(f"unexpected WAV format: {path}")
    duration = frames / sample_rate
    if abs(duration - expected_duration) > 0.08:
        raise DatasetPlanError(
            f"WAV duration mismatch for {path.name}: {duration:.3f}s"
        )
    peak = 0
    with wave.open(str(path), "rb") as stream:
        while payload := stream.readframes(8192):
            view = memoryview(payload).cast("h")
            peak = max(peak, max((abs(value) for value in view), default=0))
    if peak == 0:
        raise DatasetPlanError(f"WAV output is silent: {path}")
    return {
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration_seconds": round(duration, 6),
        "peak_pcm16": peak,
    }


def build_dataset(plan_path: Path, output_dir: Path, ffmpeg: Path) -> Path:
    plan = load_and_validate_plan(plan_path)
    ffmpeg = ffmpeg.resolve(strict=True)
    if not ffmpeg.is_file():
        raise DatasetPlanError("ffmpeg must be a file")
    version = _ffmpeg_version(ffmpeg)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise DatasetPlanError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        clips_dir = staging / "clips"
        clips_dir.mkdir()
        entries: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}
        derivations: list[dict[str, Any]] = []
        sources = plan["_resolved"]["sources"]
        for clip in plan["_resolved"]["clips"]:
            source = sources[clip["source_id"]]
            destination = clips_dir / f"{clip['clip_id']}.wav"
            command = _extract_clip(
                ffmpeg,
                source["path"],
                destination,
                clip["start_seconds"],
                clip["end_seconds"],
            )
            metadata = _wav_metadata(destination, clip["duration_seconds"])
            digest, size = _sha256(destination)
            relative = destination.relative_to(staging).as_posix()
            entries[clip["split"]].append(
                {
                    "audio": relative,
                    "text": clip["text"].strip(),
                    "duration": metadata["duration_seconds"],
                    "dataset_id": 0,
                }
            )
            derivations.append(
                {
                    "clip_id": clip["clip_id"],
                    "split": clip["split"],
                    "source_id": clip["source_id"],
                    "source_sha256": source["sha256"],
                    "start_seconds": clip["start_seconds"],
                    "end_seconds": clip["end_seconds"],
                    "text": clip["text"].strip(),
                    "review": clip["review"],
                    "output_path": relative,
                    "output_sha256": digest,
                    "output_size_bytes": size,
                    "media": metadata,
                    "command": command[1:],
                }
            )

        if not entries["train"]:
            raise DatasetPlanError("at least one train clip is required")
        train_seconds = sum(item["duration"] for item in entries["train"])
        if not MIN_TRAIN_SECONDS <= train_seconds <= MAX_TRAIN_SECONDS:
            raise DatasetPlanError(
                f"training audio must total {MIN_TRAIN_SECONDS:.0f}-{MAX_TRAIN_SECONDS:.0f}s; "
                f"got {train_seconds:.3f}s"
            )
        for split, split_entries in entries.items():
            if not split_entries:
                continue
            manifest_path = staging / f"{split}.jsonl"
            with manifest_path.open("xb") as stream:
                for entry in split_entries:
                    stream.write(
                        json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n"
                    )

        source_manifest = []
        for source in sources.values():
            digest_after, size_after = _sha256(source["path"])
            if digest_after != source["sha256"] or size_after != source["size_bytes"]:
                raise DatasetPlanError(
                    f"source changed during extraction: {source['source_id']}"
                )
            source_manifest.append(
                {
                    "source_id": source["source_id"],
                    "path": str(source["path"]),
                    "sha256": digest_after,
                    "size_bytes": size_after,
                }
            )
        review_export = _review_export_module()
        try:
            verified_review_evidence = review_export.validate_plan_review_evidence(
                raw_evidence=plan["review_evidence"],
                base=plan["_resolved"]["plan_path"].parent,
                authorization=plan["_resolved"]["authorization"],
                authorization_status=plan["authorization"]["status"],
                sources=plan["_resolved"]["sources"],
                clips=plan["_resolved"]["clips"],
            )
        except review_export.ReviewExportError as exc:
            raise DatasetPlanError(
                f"review evidence changed during dataset preparation: {exc}"
            ) from exc
        authorization = plan["_resolved"]["authorization"]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "echoweave-voxcpm2-training-dataset",
            "dataset_id": plan["dataset_id"],
            "subject_id": plan["subject_id"],
            "ready_for_local_training": True,
            "runtime_promotion_allowed": False,
            "third_party_model_processing_allowed": bool(
                plan["authorization"].get("third_party_model_processing", False)
            ),
            "authorization": {
                "status": plan["authorization"]["status"],
                "record_path": str(authorization["path"]),
                "record_sha256": authorization["sha256"],
                "record_size_bytes": authorization["size_bytes"],
            },
            "format": {
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "sample_width_bytes": 2,
                "codec": "pcm_s16le",
            },
            "ffmpeg_version": version,
            "source_files": source_manifest,
            "review_evidence": verified_review_evidence,
            "derivations": derivations,
            "statistics": {
                "train_clips": len(entries["train"]),
                "train_seconds": round(train_seconds, 6),
                "validation_clips": len(entries["validation"]),
                "validation_seconds": round(
                    sum(item["duration"] for item in entries["validation"]), 6
                ),
            },
        }
        (staging / "dataset-manifest.json").write_bytes(_json_bytes(manifest))
        os.replace(staging, output_dir)
        return output_dir / "dataset-manifest.json"
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest = build_dataset(args.plan, args.output, args.ffmpeg)
    except (DatasetPlanError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"dataset preparation failed: {exc}") from exc
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
