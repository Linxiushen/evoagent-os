"""Create explicit no-OCR evidence for a provenance-bound audio-only source."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import wave
from itertools import pairwise
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
KIND = "echoweave-audio-only-review-evidence"
TRANSCRIPT_KIND = "echoweave-qwen-range-machine-transcript"
TRANSCRIPT_SCHEMA_VERSIONS = {1, 2}
TRANSCRIPT_V1_SEGMENT_KEYS = {
    "clip_id",
    "start_seconds",
    "end_seconds",
    "duration_seconds",
    "language",
    "text",
}
TRANSCRIPT_V2_SEGMENT_KEYS = TRANSCRIPT_V1_SEGMENT_KEYS | {"language_hint"}
SAMPLE_RATE = 16_000
MAX_JSON_BYTES = 64 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class AudioOnlyEvidenceError(ValueError):
    """Raised when audio-only evidence cannot be bound without ambiguity."""


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
            raise AudioOnlyEvidenceError(
                f"{label} must not contain symbolic links or junctions"
            )


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _bound_file(path: Path, label: str) -> dict[str, Any]:
    candidate = _absolute(path)
    _reject_link_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AudioOnlyEvidenceError(f"{label} could not be resolved") from exc
    if not resolved.is_file():
        raise AudioOnlyEvidenceError(f"{label} must be a regular file")
    digest, size = _sha256(resolved)
    if size <= 0:
        raise AudioOnlyEvidenceError(f"{label} must not be empty")
    return {"path": resolved, "sha256": digest, "size_bytes": size}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AudioOnlyEvidenceError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_json(binding: dict[str, Any], label: str) -> dict[str, Any]:
    if binding["size_bytes"] > MAX_JSON_BYTES:
        raise AudioOnlyEvidenceError(f"{label} is too large")
    try:
        raw = binding["path"].read_bytes()
    except OSError as exc:
        raise AudioOnlyEvidenceError(f"{label} could not be read") from exc
    if (
        len(raw) != binding["size_bytes"]
        or hashlib.sha256(raw).hexdigest() != binding["sha256"]
    ):
        raise AudioOnlyEvidenceError(f"{label} changed while it was read")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AudioOnlyEvidenceError(f"{label} must be strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AudioOnlyEvidenceError(f"{label} must contain a JSON object")
    return value


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioOnlyEvidenceError(
            "audio must be a mono PCM16 16 kHz WAV file"
        ) from exc
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != SAMPLE_RATE
        or compression != "NONE"
        or frames <= 0
    ):
        raise AudioOnlyEvidenceError("audio must be a mono PCM16 16 kHz WAV file")
    return frames / SAMPLE_RATE


def _number(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise AudioOnlyEvidenceError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise AudioOnlyEvidenceError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise AudioOnlyEvidenceError(f"{label} must be a finite number")
    return number


def _optional_language(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or len(value) > 256
    ):
        raise AudioOnlyEvidenceError(f"{label} is invalid")
    return value


def _transcript_segments(
    payload: dict[str, Any], *, audio: dict[str, Any], duration: float
) -> list[dict[str, Any]]:
    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in TRANSCRIPT_SCHEMA_VERSIONS
        or payload.get("kind") != TRANSCRIPT_KIND
    ):
        raise AudioOnlyEvidenceError("transcript is not Qwen range evidence")
    if payload.get("source_sha256") != audio["sha256"]:
        raise AudioOnlyEvidenceError("transcript source hash does not match audio")
    source_path = payload.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip():
        raise AudioOnlyEvidenceError("transcript source path is invalid")
    try:
        claimed_source = _absolute(Path(source_path)).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AudioOnlyEvidenceError(
            "transcript source path could not be resolved"
        ) from exc
    if claimed_source != audio["path"]:
        raise AudioOnlyEvidenceError("transcript source path does not match audio")

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise AudioOnlyEvidenceError("transcript must contain segments")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_segments):
        label = f"segments[{index}]"
        if not isinstance(raw, dict):
            raise AudioOnlyEvidenceError(f"{label} must be an object")
        expected_keys = (
            TRANSCRIPT_V1_SEGMENT_KEYS
            if schema_version == 1
            else TRANSCRIPT_V2_SEGMENT_KEYS
        )
        if set(raw) != expected_keys:
            raise AudioOnlyEvidenceError(
                f"{label} fields do not match Qwen transcript schema v{schema_version}"
            )
        clip_id = raw.get("clip_id")
        if not isinstance(clip_id, str) or not CLIP_ID.fullmatch(clip_id):
            raise AudioOnlyEvidenceError(f"{label}.clip_id is invalid")
        if clip_id in seen:
            raise AudioOnlyEvidenceError(f"duplicate clip_id: {clip_id}")
        seen.add(clip_id)
        start = _number(raw.get("start_seconds"), f"{label}.start_seconds")
        end = _number(raw.get("end_seconds"), f"{label}.end_seconds")
        segment_duration = end - start
        claimed_duration = _number(
            raw.get("duration_seconds"), f"{label}.duration_seconds"
        )
        if (
            start < 0
            or not 3 <= segment_duration <= 30
            or end > duration + 1e-6
            or abs(claimed_duration - segment_duration) > 1 / SAMPLE_RATE
        ):
            raise AudioOnlyEvidenceError(f"{label} range is invalid")
        _optional_language(raw.get("language"), f"{label}.language")
        if schema_version == 2:
            _optional_language(raw.get("language_hint"), f"{label}.language_hint")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise AudioOnlyEvidenceError(f"{label}.text must be non-empty")
        result.append(
            {
                "clip_id": clip_id,
                "start_seconds": start,
                "end_seconds": end,
                "asr_text": text.strip(),
                "subtitle_text": "",
                "similarity": 0.0,
                "caption_count": 0,
            }
        )
    for previous, current in pairwise(result):
        if current["start_seconds"] < previous["start_seconds"]:
            raise AudioOnlyEvidenceError("transcript segments must be sorted")
        if current["start_seconds"] < previous["end_seconds"]:
            raise AudioOnlyEvidenceError("transcript segments must not overlap")
    return result


def _prepare_output(path: Path) -> Path:
    candidate = _absolute(path)
    if not candidate.name or candidate.name in {".", ".."}:
        raise AudioOnlyEvidenceError("output path must name a file")
    _reject_link_components(candidate, "output path")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AudioOnlyEvidenceError("output parent could not be created") from exc
    _reject_link_components(parent, "output parent")
    output = parent / candidate.name
    if output.exists():
        raise AudioOnlyEvidenceError("output already exists; refusing to overwrite it")
    return output


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise AudioOnlyEvidenceError(
                "output appeared concurrently; refusing to replace it"
            )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def prepare_audio_only_evidence(
    *, audio_path: Path, transcript_path: Path, output_path: Path
) -> Path:
    audio = _bound_file(audio_path, "audio")
    transcript = _bound_file(transcript_path, "transcript")
    if audio["path"] == transcript["path"]:
        raise AudioOnlyEvidenceError("audio and transcript must be different files")
    duration = _wav_duration(audio["path"])
    payload = _load_json(transcript, "transcript")
    alignment = _transcript_segments(payload, audio=audio, duration=duration)
    output = _prepare_output(output_path)
    if output in {audio["path"], transcript["path"]}:
        raise AudioOnlyEvidenceError("output must not overwrite an input")

    for binding, label in ((audio, "audio"), (transcript, "transcript")):
        digest, size = _sha256(binding["path"])
        if digest != binding["sha256"] or size != binding["size_bytes"]:
            raise AudioOnlyEvidenceError(f"{label} changed during preparation")
    _write_atomic(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "input": {
                "path": str(audio["path"]),
                "sha256": audio["sha256"],
                "size_bytes": audio["size_bytes"],
                "media_type": "audio/wav",
                "video_stream": False,
            },
            "method": {
                "engine": "none",
                "ocr_performed": False,
                "reason_code": "audio_only_no_video_stream",
            },
            "captions": [],
            "segments": {
                "path": str(transcript["path"]),
                "sha256": transcript["sha256"],
                "size_bytes": transcript["size_bytes"],
            },
            "alignment": alignment,
            "limitations": [
                "No video stream was available; OCR was not performed.",
                "Empty subtitle text means unavailable, not that subtitles were proven absent.",
                "ASR text remains machine-generated until human review.",
            ],
        },
    )
    print(f"wrote audio-only evidence for {len(alignment)} clips to {output}")
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        prepare_audio_only_evidence(
            audio_path=args.audio,
            transcript_path=args.transcript,
            output_path=args.output,
        )
    except (OSError, AudioOnlyEvidenceError) as exc:
        raise SystemExit(f"Audio-only evidence preparation failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
