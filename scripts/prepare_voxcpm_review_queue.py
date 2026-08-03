"""Build a provenance-bound WAV queue for manual transcript review.

The queue contains unverified ASR candidates and explicit subtitle-evidence
status. It deliberately does not create a training manifest or approve any
clip for training.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SAMPLE_RATE = 16_000
MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 30.0
BURNED_SUBTITLE_KIND = "echoweave-burned-subtitle-review-evidence"
AUDIO_ONLY_KIND = "echoweave-audio-only-review-evidence"
CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
WINDOWS_DEVICE_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class ReviewQueueError(ValueError):
    """Raised when review queue inputs are unsafe or inconsistent."""


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


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_link_components(path: Path, label: str) -> None:
    absolute = _absolute(path)
    for component in reversed((absolute, *absolute.parents)):
        if _is_link(component):
            raise ReviewQueueError(f"{label} must not contain symbolic links")


def _bound_file(path: Path, label: str) -> dict[str, Any]:
    candidate = _absolute(path)
    _reject_link_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReviewQueueError(f"{label} could not be resolved") from exc
    if not resolved.is_file():
        raise ReviewQueueError(f"{label} must be a regular file")
    digest, size = _sha256(resolved)
    return {"path": resolved, "sha256": digest, "size_bytes": size}


def _load_json(binding: dict[str, Any], label: str) -> dict[str, Any]:
    path = binding["path"]
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
            raise ReviewQueueError(f"{label} changed while it was being read")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewQueueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ReviewQueueError(f"{label} must contain a JSON object")
    return payload


def _public_binding(role: str, binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "path": str(binding["path"]),
        "sha256": binding["sha256"],
        "size_bytes": binding["size_bytes"],
    }


def _strict_number(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise ReviewQueueError(f"{label} must be a finite JSON number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ReviewQueueError(f"{label} must be a finite JSON number") from exc
    if not math.isfinite(number):
        raise ReviewQueueError(f"{label} must be a finite JSON number")
    return number


def _validate_clip_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not CLIP_ID.fullmatch(value):
        raise ReviewQueueError(f"{label} is invalid")
    if value.endswith(".") or value.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES:
        raise ReviewQueueError(f"{label} is not a portable filename")
    return value


def _validate_ranges_source_binding(
    payload: dict[str, Any], source_binding: dict[str, Any]
) -> dict[str, str]:
    source_sha256 = payload.get("source_sha256")
    if not isinstance(source_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_sha256
    ):
        raise ReviewQueueError("Qwen ranges source_sha256 must be a lowercase SHA-256")
    if source_sha256 != source_binding["sha256"]:
        raise ReviewQueueError(
            "Qwen ranges source_sha256 does not match the source audio"
        )
    evidence = {"source_sha256": source_sha256}
    if "source_path" in payload:
        source_path = payload["source_path"]
        if (
            not isinstance(source_path, str)
            or not source_path.strip()
            or "\x00" in source_path
            or len(source_path) > 4096
        ):
            raise ReviewQueueError("Qwen ranges source_path claim is invalid")
        evidence["source_path_claim"] = source_path
    return evidence


def _segment_boundary(segment: dict[str, Any], primary: str, fallback: str) -> Any:
    if primary in segment:
        return segment[primary]
    return segment.get(fallback)


def _parse_segments(
    payload: dict[str, Any], *, source_duration: float
) -> list[dict[str, Any]]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ReviewQueueError(
            "Qwen ranges JSON must contain a non-empty segments array"
        )
    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_output_names: dict[str, str] = {}
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise ReviewQueueError(f"segments[{index}] must be an object")
        clip_id = _validate_clip_id(
            raw_segment.get("clip_id"), f"segments[{index}].clip_id"
        )
        if clip_id in seen_ids:
            raise ReviewQueueError(f"duplicate clip_id: {clip_id}")
        seen_ids.add(clip_id)
        output_name = f"{clip_id}.wav".casefold()
        if output_name in seen_output_names:
            raise ReviewQueueError(
                "clip_id output filename collision: "
                f"{seen_output_names[output_name]} and {clip_id}"
            )
        seen_output_names[output_name] = clip_id
        start = _strict_number(
            _segment_boundary(raw_segment, "start_seconds", "start"),
            f"segments[{index}].start_seconds",
        )
        end = _strict_number(
            _segment_boundary(raw_segment, "end_seconds", "end"),
            f"segments[{index}].end_seconds",
        )
        duration = end - start
        if start < 0 or not MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS:
            raise ReviewQueueError(
                f"segments[{index}] duration must be "
                f"{MIN_CLIP_SECONDS}-{MAX_CLIP_SECONDS}s"
            )
        if end > source_duration + (1 / SAMPLE_RATE):
            raise ReviewQueueError(f"segments[{index}] exceeds source audio duration")
        text = raw_segment.get("text")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ReviewQueueError(f"segments[{index}].text must be non-empty")
        segments.append(
            {
                "clip_id": clip_id,
                "start_seconds": start,
                "end_seconds": end,
                "duration_seconds": duration,
                "candidate_transcript": text.strip(),
            }
        )
    ordered = sorted(
        segments, key=lambda item: (item["start_seconds"], item["clip_id"])
    )
    for previous, current in pairwise(ordered):
        if current["start_seconds"] < previous["end_seconds"]:
            raise ReviewQueueError(
                f"overlapping segments: {previous['clip_id']} and {current['clip_id']}"
            )
    return segments


def _parse_exclusions(
    values: Iterable[str], segments: list[dict[str, Any]]
) -> set[str]:
    exclusions: set[str] = set()
    for index, value in enumerate(values):
        clip_id = _validate_clip_id(value, f"exclude_clip_ids[{index}]")
        if clip_id in exclusions:
            raise ReviewQueueError(f"duplicate excluded clip_id: {clip_id}")
        exclusions.add(clip_id)
    known_ids = {segment["clip_id"] for segment in segments}
    unknown = exclusions - known_ids
    if unknown:
        raise ReviewQueueError(
            f"unknown excluded clip_id: {', '.join(sorted(unknown))}"
        )
    return exclusions


def _validate_review_evidence(
    payload: dict[str, Any],
    *,
    ranges_sha256: str,
    source_sha256: str,
    selected_segments: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != SCHEMA_VERSION
    ):
        raise ReviewQueueError(
            f"review evidence schema_version must be {SCHEMA_VERSION}"
        )
    kind = payload.get("kind")
    if kind == BURNED_SUBTITLE_KIND:
        evidence_status = {
            "kind": kind,
            "ocr_performed": True,
            "reason_code": None,
        }
    elif kind == AUDIO_ONLY_KIND:
        input_metadata = payload.get("input")
        if not isinstance(input_metadata, dict):
            raise ReviewQueueError("audio-only evidence input must be an object")
        if (
            input_metadata.get("sha256") != source_sha256
            or input_metadata.get("media_type") != "audio/wav"
            or input_metadata.get("video_stream") is not False
        ):
            raise ReviewQueueError("audio-only evidence does not match source audio")
        method = payload.get("method")
        if (
            not isinstance(method, dict)
            or method.get("engine") != "none"
            or method.get("ocr_performed") is not False
            or method.get("reason_code") != "audio_only_no_video_stream"
            or payload.get("captions") != []
        ):
            raise ReviewQueueError("audio-only evidence method is invalid")
        evidence_status = {
            "kind": kind,
            "ocr_performed": False,
            "reason_code": "audio_only_no_video_stream",
        }
    else:
        raise ReviewQueueError("review evidence kind is unsupported")
    ranges_binding = payload.get("segments")
    if not isinstance(ranges_binding, dict):
        raise ReviewQueueError("review evidence is not bound to a segments file")
    if ranges_binding.get("sha256") != ranges_sha256:
        raise ReviewQueueError("review evidence does not match the Qwen ranges SHA-256")
    raw_alignment = payload.get("alignment")
    if not isinstance(raw_alignment, list):
        raise ReviewQueueError("review evidence must contain an alignment array")
    alignment: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(raw_alignment):
        if not isinstance(raw_item, dict):
            raise ReviewQueueError(f"OCR alignment[{index}] must be an object")
        clip_id = _validate_clip_id(
            raw_item.get("clip_id"), f"OCR alignment[{index}].clip_id"
        )
        if clip_id in alignment:
            raise ReviewQueueError(f"duplicate OCR alignment clip_id: {clip_id}")
        subtitle_text = raw_item.get("subtitle_text")
        if not isinstance(subtitle_text, str) or "\x00" in subtitle_text:
            raise ReviewQueueError(
                f"OCR alignment[{index}].subtitle_text must be a string"
            )
        similarity = _strict_number(
            raw_item.get("similarity"), f"OCR alignment[{index}].similarity"
        )
        if not 0 <= similarity <= 1:
            raise ReviewQueueError(
                f"OCR alignment[{index}].similarity must be between 0 and 1"
            )
        caption_count = raw_item.get("caption_count")
        if type(caption_count) is not int or caption_count < 0:
            raise ReviewQueueError(
                f"OCR alignment[{index}].caption_count must be a non-negative integer"
            )
        if kind == AUDIO_ONLY_KIND and (
            subtitle_text != "" or similarity != 0.0 or caption_count != 0
        ):
            raise ReviewQueueError(
                "audio-only evidence must not claim subtitle observations"
            )
        alignment[clip_id] = {
            "start_seconds": _strict_number(
                _segment_boundary(raw_item, "start_seconds", "start"),
                f"OCR alignment[{index}].start_seconds",
            ),
            "end_seconds": _strict_number(
                _segment_boundary(raw_item, "end_seconds", "end"),
                f"OCR alignment[{index}].end_seconds",
            ),
            "asr_text": raw_item.get("asr_text"),
            "subtitle_text": subtitle_text,
            "similarity": similarity,
            "caption_count": caption_count,
        }
    selected_evidence: dict[str, dict[str, Any]] = {}
    for segment in selected_segments:
        clip_id = segment["clip_id"]
        evidence = alignment.get(clip_id)
        if evidence is None:
            raise ReviewQueueError(f"review evidence is missing clip_id: {clip_id}")
        if (
            abs(evidence["start_seconds"] - segment["start_seconds"]) > 0.001
            or abs(evidence["end_seconds"] - segment["end_seconds"]) > 0.001
        ):
            raise ReviewQueueError(f"OCR range does not match clip_id: {clip_id}")
        if (
            not isinstance(evidence["asr_text"], str)
            or evidence["asr_text"].strip() != segment["candidate_transcript"]
        ):
            raise ReviewQueueError(f"OCR ASR text does not match clip_id: {clip_id}")
        selected_evidence[clip_id] = {
            "subtitle_text": evidence["subtitle_text"],
            "similarity": evidence["similarity"],
            "caption_count": evidence["caption_count"],
        }
    return evidence_status, selected_evidence


def _source_audio_metadata(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise ReviewQueueError("source audio must be a PCM WAV file") from exc
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != SAMPLE_RATE
        or compression != "NONE"
        or frames <= 0
    ):
        raise ReviewQueueError("source audio must be mono 16-bit 16kHz PCM WAV")
    return {
        "container": "wav",
        "codec": "pcm_s16le",
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration_seconds": frames / sample_rate,
    }


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
        raise ReviewQueueError("ffmpeg did not return a version")
    return result.stdout.splitlines()[0].strip()


def _extract_clip(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    start: float,
    duration: float,
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
        f"{start:.6f}",
        "-t",
        f"{duration:.6f}",
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
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise ReviewQueueError(f"ffmpeg failed for {destination.name}: {detail}")
    return command


def _output_wav_metadata(path: Path, expected_duration: float) -> dict[str, Any]:
    metadata = _source_audio_metadata(path)
    if abs(metadata["duration_seconds"] - expected_duration) > 0.02:
        raise ReviewQueueError(f"WAV duration mismatch for {path.name}")
    metadata["duration_seconds"] = round(metadata["duration_seconds"], 6)
    return metadata


def _contained_path(root: Path, relative: Path) -> Path:
    candidate = _absolute(root / relative)
    try:
        candidate.relative_to(_absolute(root))
    except ValueError as exc:
        raise ReviewQueueError(
            f"output path escapes staging directory: {relative}"
        ) from exc
    return candidate


def _manifest_command(command: list[str], source: Path, destination: Path) -> list[str]:
    replacements = {
        str(source): "${SOURCE_AUDIO}",
        str(destination): "${OUTPUT_WAV}",
    }
    return [replacements.get(argument, argument) for argument in command[1:]]


def _verify_unchanged(binding: dict[str, Any], label: str) -> None:
    _reject_link_components(binding["path"], label)
    try:
        digest, size = _sha256(binding["path"])
    except OSError as exc:
        raise ReviewQueueError(
            f"{label} changed while the queue was being built"
        ) from exc
    if digest != binding["sha256"] or size != binding["size_bytes"]:
        raise ReviewQueueError(f"{label} changed while the queue was being built")


def build_review_queue(
    *,
    ranges_path: Path,
    ocr_path: Path,
    audio_path: Path,
    authorization_path: Path,
    output_dir: Path,
    ffmpeg: Path,
    exclude_clip_ids: Iterable[str] = (),
) -> Path:
    output_dir = _absolute(output_dir)
    _reject_link_components(output_dir, "output path")
    if output_dir.exists():
        raise ReviewQueueError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_components(output_dir.parent, "output parent")

    bindings = {
        "qwen_ranges": _bound_file(ranges_path, "Qwen ranges JSON"),
        "burned_subtitle_ocr": _bound_file(ocr_path, "OCR evidence JSON"),
        "source_audio": _bound_file(audio_path, "source audio"),
        "authorization_record": _bound_file(authorization_path, "authorization record"),
    }
    ranges = _load_json(bindings["qwen_ranges"], "Qwen ranges JSON")
    evidence = _load_json(bindings["burned_subtitle_ocr"], "review evidence JSON")
    ranges_source_evidence = _validate_ranges_source_binding(
        ranges, bindings["source_audio"]
    )
    source_media = _source_audio_metadata(bindings["source_audio"]["path"])
    segments = _parse_segments(ranges, source_duration=source_media["duration_seconds"])
    exclusions = _parse_exclusions(exclude_clip_ids, segments)
    selected = [item for item in segments if item["clip_id"] not in exclusions]
    if not selected:
        raise ReviewQueueError("all clips were excluded")
    evidence_status, evidence_by_clip = _validate_review_evidence(
        evidence,
        ranges_sha256=bindings["qwen_ranges"]["sha256"],
        source_sha256=bindings["source_audio"]["sha256"],
        selected_segments=selected,
    )

    ffmpeg_binding = _bound_file(ffmpeg, "ffmpeg")
    ffmpeg_version = _ffmpeg_version(ffmpeg_binding["path"])
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        clips_dir = _contained_path(staging, Path("clips"))
        clips_dir.mkdir()
        queue_items: list[dict[str, Any]] = []
        for segment in selected:
            relative = Path("clips") / f"{segment['clip_id']}.wav"
            destination = _contained_path(staging, relative)
            command = _extract_clip(
                ffmpeg_binding["path"],
                bindings["source_audio"]["path"],
                destination,
                segment["start_seconds"],
                segment["duration_seconds"],
            )
            media = _output_wav_metadata(destination, segment["duration_seconds"])
            digest, size = _sha256(destination)
            queue_items.append(
                {
                    "clip_id": segment["clip_id"],
                    "audio_path": relative.as_posix(),
                    "audio_sha256": digest,
                    "audio_size_bytes": size,
                    "start_seconds": segment["start_seconds"],
                    "end_seconds": segment["end_seconds"],
                    "duration_seconds": media["duration_seconds"],
                    "candidate_transcript": segment["candidate_transcript"],
                    "ocr_evidence": evidence_by_clip[segment["clip_id"]],
                    "human_review": False,
                    "approved": False,
                    "approved_for_training": False,
                    "transcript_verified": False,
                    "media": media,
                    "derivation_command": _manifest_command(
                        command, bindings["source_audio"]["path"], destination
                    ),
                }
            )

        for label, binding in bindings.items():
            _verify_unchanged(binding, label.replace("_", " "))
        _verify_unchanged(ffmpeg_binding, "ffmpeg")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "echoweave-voxcpm2-manual-review-queue",
            "review_only": True,
            "training_ready": False,
            "ready_for_local_training": False,
            "runtime_promotion_allowed": False,
            "human_review": False,
            "approved": False,
            "approved_for_training": False,
            "transcript_verified": False,
            "inputs": [
                _public_binding(role, binding) for role, binding in bindings.items()
            ],
            "qwen_source_evidence": ranges_source_evidence,
            "review_evidence": evidence_status,
            "source_audio": source_media,
            "excluded_clip_ids": sorted(exclusions),
            "ffmpeg": {
                **_public_binding("media_extractor", ffmpeg_binding),
                "version": ffmpeg_version,
            },
            "clips": queue_items,
            "limitations": [
                "ASR text is a machine-generated candidate, not a verified transcript.",
                (
                    "OCR text is supporting evidence and is not human approval."
                    if evidence_status["ocr_performed"]
                    else "OCR was not performed because the source has no video stream."
                ),
                "No clip in this queue is approved for training.",
            ],
        }
        manifest_path = _contained_path(staging, Path("review-queue.json"))
        with manifest_path.open("xb") as stream:
            stream.write(_json_bytes(manifest))
        if output_dir.exists() or _is_link(output_dir):
            raise ReviewQueueError(f"output directory already exists: {output_dir}")
        try:
            staging.rename(output_dir)
        except FileExistsError as exc:
            raise ReviewQueueError(
                f"output directory already exists: {output_dir}"
            ) from exc
        return output_dir / "review-queue.json"
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranges", type=Path, required=True)
    parser.add_argument("--ocr-evidence", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--exclude-clip-id", action="append", default=[])
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest = build_review_queue(
            ranges_path=args.ranges,
            ocr_path=args.ocr_evidence,
            audio_path=args.audio,
            authorization_path=args.authorization,
            output_dir=args.output,
            ffmpeg=args.ffmpeg,
            exclude_clip_ids=args.exclude_clip_id,
        )
    except (OSError, subprocess.SubprocessError, ReviewQueueError) as exc:
        raise SystemExit(f"review queue preparation failed: {exc}") from exc
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
