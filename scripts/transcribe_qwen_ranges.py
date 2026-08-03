"""Transcribe provenance-bound WAV ranges with a local Qwen3-ASR checkout.

The output is machine-generated transcript evidence. It intentionally contains
no review or approval fields and can only resume from an exact completed prefix.
"""

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

RANGES_SCHEMA_VERSION = 1
OUTPUT_SCHEMA_VERSION = 2
KIND = "echoweave-qwen-range-machine-transcript"
SAMPLE_RATE = 16_000
MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 30.0
MAX_JSON_BYTES = 64 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MODEL_REVISION = re.compile(r"[0-9a-f]{40}\Z")
CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
WINDOWS_DEVICE_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
RANGES_KEYS = {"schema_version", "source_sha256", "segments"}
RANGE_SEGMENT_KEYS = {"clip_id", "start_seconds", "end_seconds"}
OUTPUT_KEYS = {
    "schema_version",
    "kind",
    "source_path",
    "source_sha256",
    "ranges_path",
    "ranges_sha256",
    "model",
    "model_revision",
    "model_path",
    "device",
    "segments",
}
LEGACY_OUTPUT_SEGMENT_KEYS = {
    "clip_id",
    "start_seconds",
    "end_seconds",
    "duration_seconds",
    "language",
    "text",
}
OUTPUT_SEGMENT_KEYS = LEGACY_OUTPUT_SEGMENT_KEYS | {"language_hint"}


class QwenRangeTranscriptionError(ValueError):
    """Raised when range transcription cannot proceed without ambiguity."""


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
            raise QwenRangeTranscriptionError(
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
        raise QwenRangeTranscriptionError(f"{label} could not be resolved") from exc
    if not resolved.is_file():
        raise QwenRangeTranscriptionError(f"{label} must be a regular file")
    digest, size = _sha256(resolved)
    return {"path": resolved, "sha256": digest, "size_bytes": size}


def _bound_directory(path: Path, label: str) -> Path:
    candidate = _absolute(path)
    _reject_link_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise QwenRangeTranscriptionError(f"{label} could not be resolved") from exc
    if not resolved.is_dir():
        raise QwenRangeTranscriptionError(f"{label} must be a directory")
    return resolved


def _prepare_output_path(path: Path) -> Path:
    candidate = _absolute(path)
    if not candidate.name or candidate.name in {".", ".."}:
        raise QwenRangeTranscriptionError("output path must name a file")
    _reject_link_components(candidate, "output path")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise QwenRangeTranscriptionError(
            "output parent could not be created or resolved"
        ) from exc
    _reject_link_components(parent, "output parent")
    if not parent.is_dir():
        raise QwenRangeTranscriptionError("output parent must be a directory")
    output = parent / candidate.name
    _reject_link_components(output, "output path")
    if output.exists() and not output.is_file():
        raise QwenRangeTranscriptionError("output must be a regular file")
    return output


def _verify_file_unchanged(binding: dict[str, Any], label: str) -> None:
    _reject_link_components(binding["path"], label)
    try:
        current_path = binding["path"].resolve(strict=True)
        digest, size = _sha256(current_path)
    except (OSError, RuntimeError) as exc:
        raise QwenRangeTranscriptionError(
            f"{label} changed during transcription"
        ) from exc
    if (
        current_path != binding["path"]
        or digest != binding["sha256"]
        or size != binding["size_bytes"]
    ):
        raise QwenRangeTranscriptionError(f"{label} changed during transcription")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QwenRangeTranscriptionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise QwenRangeTranscriptionError(f"invalid JSON numeric constant: {value}")


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    if len(raw) > MAX_JSON_BYTES:
        raise QwenRangeTranscriptionError(f"{label} is too large")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QwenRangeTranscriptionError(f"{label} must be strict UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise QwenRangeTranscriptionError(f"{label} must contain a JSON object")
    return payload


def _load_bound_json(binding: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        raw = binding["path"].read_bytes()
    except OSError as exc:
        raise QwenRangeTranscriptionError(f"{label} could not be read") from exc
    if (
        hashlib.sha256(raw).hexdigest() != binding["sha256"]
        or len(raw) != binding["size_bytes"]
    ):
        raise QwenRangeTranscriptionError(f"{label} changed while it was read")
    return _decode_json(raw, label)


def _strict_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing:
        raise QwenRangeTranscriptionError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise QwenRangeTranscriptionError(
            f"{label} contains unsupported fields: {', '.join(sorted(extra))}"
        )


def _strict_number(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise QwenRangeTranscriptionError(f"{label} must be a finite JSON number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise QwenRangeTranscriptionError(
            f"{label} must be a finite JSON number"
        ) from exc
    if not math.isfinite(number):
        raise QwenRangeTranscriptionError(f"{label} must be a finite JSON number")
    return number


def _validate_clip_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not CLIP_ID.fullmatch(value):
        raise QwenRangeTranscriptionError(f"{label} is invalid")
    if value.endswith(".") or value.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES:
        raise QwenRangeTranscriptionError(f"{label} is not a portable filename")
    return value


def _wav_metadata(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise QwenRangeTranscriptionError(
            "audio must be a mono PCM16 16kHz WAV file"
        ) from exc
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != SAMPLE_RATE
        or compression != "NONE"
        or frames <= 0
    ):
        raise QwenRangeTranscriptionError("audio must be a mono PCM16 16kHz WAV file")
    return {
        "frames": frames,
        "duration_seconds": frames / SAMPLE_RATE,
    }


def _parse_ranges(
    payload: dict[str, Any],
    *,
    source_sha256: str,
    source_duration: float,
) -> list[dict[str, Any]]:
    _strict_keys(payload, RANGES_KEYS, "ranges JSON")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != RANGES_SCHEMA_VERSION
    ):
        raise QwenRangeTranscriptionError(
            f"ranges schema_version must be {RANGES_SCHEMA_VERSION}"
        )
    claimed_sha256 = payload["source_sha256"]
    if not isinstance(claimed_sha256, str) or not SHA256.fullmatch(claimed_sha256):
        raise QwenRangeTranscriptionError(
            "ranges source_sha256 must be a lowercase SHA-256"
        )
    if claimed_sha256 != source_sha256:
        raise QwenRangeTranscriptionError(
            "ranges source_sha256 does not match the source audio"
        )
    raw_segments = payload["segments"]
    if not isinstance(raw_segments, list) or not raw_segments:
        raise QwenRangeTranscriptionError(
            "ranges JSON must contain a non-empty segments array"
        )

    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    for index, raw_segment in enumerate(raw_segments):
        label = f"segments[{index}]"
        if not isinstance(raw_segment, dict):
            raise QwenRangeTranscriptionError(f"{label} must be an object")
        _strict_keys(raw_segment, RANGE_SEGMENT_KEYS, label)
        clip_id = _validate_clip_id(raw_segment["clip_id"], f"{label}.clip_id")
        if clip_id in seen_ids:
            raise QwenRangeTranscriptionError(f"duplicate clip_id: {clip_id}")
        seen_ids.add(clip_id)
        output_name = f"{clip_id}.wav".casefold()
        if output_name in seen_names:
            raise QwenRangeTranscriptionError(
                "case-insensitive clip filename collision: "
                f"{seen_names[output_name]} and {clip_id}"
            )
        seen_names[output_name] = clip_id

        start = _strict_number(raw_segment["start_seconds"], f"{label}.start_seconds")
        end = _strict_number(raw_segment["end_seconds"], f"{label}.end_seconds")
        duration = end - start
        if start < 0 or not MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS:
            raise QwenRangeTranscriptionError(
                f"{label} duration must be {MIN_CLIP_SECONDS}-{MAX_CLIP_SECONDS}s"
            )
        if end > source_duration:
            raise QwenRangeTranscriptionError(f"{label} exceeds source audio duration")
        segments.append(
            {
                "clip_id": clip_id,
                "start_seconds": start,
                "end_seconds": end,
                "duration_seconds": duration,
            }
        )

    for previous, current in pairwise(segments):
        if current["start_seconds"] < previous["start_seconds"]:
            raise QwenRangeTranscriptionError(
                "ranges segments must be sorted by start_seconds"
            )
        if current["start_seconds"] < previous["end_seconds"]:
            raise QwenRangeTranscriptionError(
                f"overlapping segments: {previous['clip_id']} and {current['clip_id']}"
            )
    return segments


def _identity(
    *,
    audio_binding: dict[str, Any],
    ranges_binding: dict[str, Any],
    model_path: Path,
    model_id: str,
    model_revision: str,
    device: str,
) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "kind": KIND,
        "source_path": str(audio_binding["path"]),
        "source_sha256": audio_binding["sha256"],
        "ranges_path": str(ranges_binding["path"]),
        "ranges_sha256": ranges_binding["sha256"],
        "model": model_id,
        "model_revision": model_revision,
        "model_path": str(model_path),
        "device": device,
    }


def _validate_text_field(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > 1_000_000
    ):
        raise QwenRangeTranscriptionError(f"{label} must be non-empty text")
    return value.strip()


def _validate_language_hint(value: Any, label: str) -> str | None:
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
        raise QwenRangeTranscriptionError(f"{label} is invalid")
    return value


def _validate_existing_output(
    payload: dict[str, Any],
    *,
    identity: dict[str, Any],
    requested_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _strict_keys(payload, OUTPUT_KEYS, "existing output")
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version not in {
        1,
        OUTPUT_SCHEMA_VERSION,
    }:
        raise QwenRangeTranscriptionError(
            "existing output has an unsupported schema_version"
        )
    for key, expected in identity.items():
        if key == "schema_version":
            continue
        if type(payload[key]) is not type(expected) or payload[key] != expected:
            raise QwenRangeTranscriptionError(
                f"existing output has an incompatible {key} binding"
            )
    raw_segments = payload["segments"]
    if not isinstance(raw_segments, list):
        raise QwenRangeTranscriptionError("existing output segments must be an array")
    if len(raw_segments) > len(requested_segments):
        raise QwenRangeTranscriptionError(
            "existing output contains more segments than requested"
        )

    validated: list[dict[str, Any]] = []
    segment_keys = (
        LEGACY_OUTPUT_SEGMENT_KEYS if schema_version == 1 else OUTPUT_SEGMENT_KEYS
    )
    for index, raw_segment in enumerate(raw_segments):
        label = f"existing output segments[{index}]"
        if not isinstance(raw_segment, dict):
            raise QwenRangeTranscriptionError(f"{label} must be an object")
        _strict_keys(raw_segment, segment_keys, label)
        expected = requested_segments[index]
        for key in (
            "clip_id",
            "start_seconds",
            "end_seconds",
            "duration_seconds",
        ):
            if (
                type(raw_segment[key]) is not type(expected[key])
                or raw_segment[key] != expected[key]
            ):
                raise QwenRangeTranscriptionError(
                    f"{label} is not the exact requested prefix at field {key}"
                )
        language = raw_segment["language"]
        if language is not None and (
            not isinstance(language, str)
            or not language.strip()
            or "\x00" in language
            or len(language) > 256
        ):
            raise QwenRangeTranscriptionError(f"{label}.language is invalid")
        validated.append(
            {
                **expected,
                "language": language.strip() if isinstance(language, str) else None,
                "text": _validate_text_field(raw_segment["text"], f"{label}.text"),
                "language_hint": (
                    None
                    if schema_version == 1
                    else _validate_language_hint(
                        raw_segment["language_hint"], f"{label}.language_hint"
                    )
                ),
            }
        )
    return validated


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _current_output_digest(output: Path) -> str | None:
    _reject_link_components(output, "output path")
    if not output.exists():
        return None
    if not output.is_file():
        raise QwenRangeTranscriptionError("output must be a regular file")
    digest, _ = _sha256(output)
    return digest


def _atomic_checkpoint(
    output: Path,
    payload: dict[str, Any],
    *,
    expected_digest: str | None,
) -> str:
    current_digest = _current_output_digest(output)
    if current_digest != expected_digest:
        raise QwenRangeTranscriptionError(
            "output changed concurrently; refusing to overwrite it"
        )
    data = _json_bytes(payload)
    digest = hashlib.sha256(data).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _current_output_digest(output) != expected_digest:
            raise QwenRangeTranscriptionError(
                "output changed concurrently; refusing to overwrite it"
            )
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return digest


def _read_segment_pcm(path: Path, segment: dict[str, Any]) -> bytes:
    start_frame = round(segment["start_seconds"] * SAMPLE_RATE)
    end_frame = round(segment["end_seconds"] * SAMPLE_RATE)
    frame_count = end_frame - start_frame
    if start_frame < 0 or frame_count <= 0:
        raise QwenRangeTranscriptionError(
            f"invalid frame range for clip_id: {segment['clip_id']}"
        )
    try:
        with wave.open(str(path), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getframerate() != SAMPLE_RATE
                or stream.getcomptype() != "NONE"
                or end_frame > stream.getnframes()
            ):
                raise QwenRangeTranscriptionError(
                    "audio changed format during transcription"
                )
            stream.setpos(start_frame)
            pcm = stream.readframes(frame_count)
    except (OSError, EOFError, wave.Error) as exc:
        raise QwenRangeTranscriptionError(
            f"could not read audio for clip_id: {segment['clip_id']}"
        ) from exc
    if len(pcm) != frame_count * 2:
        raise QwenRangeTranscriptionError(
            f"audio was truncated for clip_id: {segment['clip_id']}"
        )
    return pcm


def _load_model(model_path: Path, model_revision: str, device: str) -> Any:
    try:
        import torch
        from qwen_asr import Qwen3ASRModel
    except ImportError as exc:
        raise QwenRangeTranscriptionError(
            "Qwen ASR is not installed; install the qwen optional dependency"
        ) from exc
    dtype = torch.float32 if device == "cpu" or device == "mps" else torch.bfloat16
    try:
        return Qwen3ASRModel.from_pretrained(
            str(model_path),
            revision=model_revision,
            local_files_only=True,
            dtype=dtype,
            device_map=device,
            max_inference_batch_size=1,
            max_new_tokens=256,
        )
    except Exception as exc:
        raise QwenRangeTranscriptionError(
            "could not load the pinned local Qwen ASR model"
        ) from exc


def _transcribe_segment(
    model: Any, pcm: bytes, language_hint: str | None
) -> tuple[str | None, str]:
    try:
        import numpy as np
    except ImportError as exc:
        raise QwenRangeTranscriptionError(
            "Qwen ASR transcription requires numpy"
        ) from exc
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    try:
        results = model.transcribe(audio=(audio, SAMPLE_RATE), language=language_hint)
    except Exception as exc:
        raise QwenRangeTranscriptionError("Qwen ASR transcription failed") from exc
    if not isinstance(results, (list, tuple)) or len(results) != 1:
        raise QwenRangeTranscriptionError(
            "Qwen ASR must return exactly one result per segment"
        )
    result = results[0]
    if isinstance(result, dict):
        raw_text = result.get("text")
        raw_language = result.get("language")
    else:
        raw_text = getattr(result, "text", None)
        raw_language = getattr(result, "language", None)
    text = _validate_text_field(raw_text, "Qwen ASR result text")
    if raw_language is not None and (
        not isinstance(raw_language, str)
        or not raw_language.strip()
        or "\x00" in raw_language
        or len(raw_language) > 256
    ):
        raise QwenRangeTranscriptionError("Qwen ASR result language is invalid")
    language = raw_language.strip() if isinstance(raw_language, str) else None
    return language, text


def _validate_identifier(value: str, label: str, max_length: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or len(value) > max_length
    ):
        raise QwenRangeTranscriptionError(f"{label} is invalid")
    return value


def transcribe_ranges(
    *,
    audio_path: Path,
    ranges_path: Path,
    model_path: Path,
    model_id: str,
    model_revision: str,
    output_path: Path,
    device: str = "cpu",
    language: str | None = None,
) -> Path:
    model_id = _validate_identifier(model_id, "model ID")
    device = _validate_identifier(device, "device", max_length=128)
    language_hint = _validate_language_hint(language, "language hint")
    if not isinstance(model_revision, str) or not MODEL_REVISION.fullmatch(
        model_revision
    ):
        raise QwenRangeTranscriptionError(
            "model revision must be a lowercase 40-character Git commit"
        )

    audio_binding = _bound_file(audio_path, "source audio")
    ranges_binding = _bound_file(ranges_path, "ranges JSON")
    local_model_path = _bound_directory(model_path, "model path")
    ranges_payload = _load_bound_json(ranges_binding, "ranges JSON")
    audio_metadata = _wav_metadata(audio_binding["path"])
    requested_segments = _parse_ranges(
        ranges_payload,
        source_sha256=audio_binding["sha256"],
        source_duration=audio_metadata["duration_seconds"],
    )
    _verify_file_unchanged(audio_binding, "source audio")
    _verify_file_unchanged(ranges_binding, "ranges JSON")

    output = _prepare_output_path(output_path)
    if output in {audio_binding["path"], ranges_binding["path"]}:
        raise QwenRangeTranscriptionError("output must not overwrite an input file")
    identity = _identity(
        audio_binding=audio_binding,
        ranges_binding=ranges_binding,
        model_path=local_model_path,
        model_id=model_id,
        model_revision=model_revision,
        device=device,
    )

    output_digest = _current_output_digest(output)
    completed_segments: list[dict[str, Any]] = []
    if output_digest is not None:
        output_binding = _bound_file(output, "existing output")
        output_payload = _load_bound_json(output_binding, "existing output")
        completed_segments = _validate_existing_output(
            output_payload,
            identity=identity,
            requested_segments=requested_segments,
        )
        output_digest = output_binding["sha256"]

    if len(completed_segments) == len(requested_segments):
        return output

    model = _load_model(local_model_path, model_revision, device)
    payload = {**identity, "segments": completed_segments}
    for segment in requested_segments[len(completed_segments) :]:
        _verify_file_unchanged(audio_binding, "source audio")
        _verify_file_unchanged(ranges_binding, "ranges JSON")
        pcm = _read_segment_pcm(audio_binding["path"], segment)
        detected_language, text = _transcribe_segment(model, pcm, language_hint)
        _verify_file_unchanged(audio_binding, "source audio")
        _verify_file_unchanged(ranges_binding, "ranges JSON")
        completed_segments.append(
            {
                **segment,
                "language": detected_language,
                "text": text,
                "language_hint": language_hint,
            }
        )
        output_digest = _atomic_checkpoint(
            output,
            payload,
            expected_digest=output_digest,
        )
        print(
            f"checkpointed {len(completed_segments)}/{len(requested_segments)} "
            f"({segment['clip_id']})",
            file=os.sys.stderr,
            flush=True,
        )
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--ranges", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--language",
        help="Optional Qwen decoding language hint recorded on every new segment.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        output = transcribe_ranges(
            audio_path=args.audio,
            ranges_path=args.ranges,
            model_path=args.model_path,
            model_id=args.model_id,
            model_revision=args.model_revision,
            output_path=args.output,
            device=args.device,
            language=args.language,
        )
    except (OSError, QwenRangeTranscriptionError) as exc:
        raise SystemExit(f"Qwen range transcription failed: {exc}") from exc
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
