"""Create provenance-bound VoxCPM candidate ranges with pinned Silero VAD."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import wave
from pathlib import Path
from typing import Any, Protocol

from echoweave.adapters.vad import SileroV5VAD
from echoweave.contracts import VADDecision

SCHEMA_VERSION = 1
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512
MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 30.0
CLIP_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
WINDOWS_DEVICE_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class SegmentationError(ValueError):
    """Raised when deterministic segmentation cannot proceed safely."""


class StreamingVAD(Protocol):
    min_silence_samples: int
    speech_pad_samples: int

    async def process(self, pcm16: bytes, sample_rate: int) -> VADDecision: ...


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
            raise SegmentationError(f"{label} must not contain links or junctions")


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _bind_wav(path: Path) -> dict[str, Any]:
    candidate = _absolute(path)
    _reject_link_components(candidate, "source audio")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SegmentationError("source audio could not be resolved") from exc
    if not resolved.is_file():
        raise SegmentationError("source audio must be a regular file")
    try:
        with wave.open(str(resolved), "rb") as stream:
            metadata = {
                "channels": stream.getnchannels(),
                "sample_width": stream.getsampwidth(),
                "sample_rate": stream.getframerate(),
                "frame_count": stream.getnframes(),
                "compression": stream.getcomptype(),
            }
    except (OSError, EOFError, wave.Error) as exc:
        raise SegmentationError(
            "source audio must be a mono PCM16 16 kHz WAV file"
        ) from exc
    if (
        metadata["channels"] != 1
        or metadata["sample_width"] != 2
        or metadata["sample_rate"] != SAMPLE_RATE
        or metadata["compression"] != "NONE"
        or metadata["frame_count"] <= 0
    ):
        raise SegmentationError("source audio must be a mono PCM16 16 kHz WAV file")
    digest, size = _sha256(resolved)
    return {"path": resolved, "sha256": digest, "size_bytes": size, **metadata}


def _read_pcm(binding: dict[str, Any]) -> bytes:
    try:
        with wave.open(str(binding["path"]), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getframerate() != SAMPLE_RATE
                or stream.getcomptype() != "NONE"
                or stream.getnframes() != binding["frame_count"]
            ):
                raise SegmentationError("source audio changed during segmentation")
            pcm = stream.readframes(binding["frame_count"])
    except (OSError, EOFError, wave.Error) as exc:
        raise SegmentationError("source audio changed during segmentation") from exc
    if len(pcm) != binding["frame_count"] * 2:
        raise SegmentationError("source audio was truncated during segmentation")
    return pcm


def _validate_seconds(value: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SegmentationError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise SegmentationError(f"{label} must be a finite number")
    return number


async def _detect_raw_spans(
    pcm: bytes,
    *,
    frame_count: int,
    vad: StreamingVAD,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    active_start: int | None = None
    current_sample = 0
    for offset in range(0, len(pcm), FRAME_SAMPLES * 2):
        frame = pcm[offset : offset + FRAME_SAMPLES * 2]
        frame_samples = len(frame) // 2
        if frame_samples < FRAME_SAMPLES:
            frame += b"\x00\x00" * (FRAME_SAMPLES - frame_samples)
        decision = await vad.process(frame, SAMPLE_RATE)
        current_sample += FRAME_SAMPLES
        if decision.speech_started and active_start is None:
            active_start = max(
                0, current_sample - FRAME_SAMPLES - vad.speech_pad_samples
            )
        if decision.speech_ended and active_start is not None:
            end = min(
                frame_count,
                current_sample - vad.min_silence_samples + vad.speech_pad_samples,
            )
            if end > active_start:
                spans.append((active_start, end))
            active_start = None
    if active_start is not None:
        spans.append((active_start, frame_count))
    return spans


def _merge_spans(
    spans: list[tuple[int, int]], *, max_gap_samples: int
) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if start < 0 or end <= start:
            raise SegmentationError("VAD returned an invalid speech span")
        if merged and start - merged[-1][1] <= max_gap_samples:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _split_and_filter_spans(
    spans: list[tuple[int, int]],
    *,
    min_samples: int,
    max_samples: int,
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for raw_start, raw_end in spans:
        start = raw_start
        while raw_end - start > max_samples:
            remaining_after_max = raw_end - (start + max_samples)
            if 0 < remaining_after_max < min_samples:
                split = raw_end - min_samples
            else:
                split = start + max_samples
            if split - start < min_samples:
                break
            result.append((start, split))
            start = split
        if raw_end - start >= min_samples:
            result.append((start, raw_end))
    return result


def _validate_prefix(value: str) -> str:
    if not isinstance(value, str) or not CLIP_PREFIX.fullmatch(value):
        raise SegmentationError("clip prefix contains unsupported characters")
    if value.endswith(".") or value.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES:
        raise SegmentationError("clip prefix is not a portable filename")
    return value


def _prepare_output(path: Path) -> Path:
    candidate = _absolute(path)
    if not candidate.name or candidate.name in {".", ".."}:
        raise SegmentationError("output path must name a file")
    _reject_link_components(candidate, "output path")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SegmentationError("output parent could not be created") from exc
    _reject_link_components(parent, "output parent")
    output = parent / candidate.name
    if output.exists():
        raise SegmentationError("output already exists; refusing to overwrite it")
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
            raise SegmentationError(
                "output appeared concurrently; refusing to replace it"
            )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


async def segment_audio(
    *,
    audio_path: Path,
    output_path: Path,
    clip_prefix: str,
    model_path: Path | None = None,
    threshold: float = 0.5,
    min_silence_ms: int = 550,
    speech_pad_ms: int = 96,
    merge_gap_ms: int = 800,
    min_clip_seconds: float = MIN_CLIP_SECONDS,
    max_clip_seconds: float = MAX_CLIP_SECONDS,
    vad: StreamingVAD | None = None,
) -> Path:
    prefix = _validate_prefix(clip_prefix)
    threshold = _validate_seconds(threshold, "threshold")
    min_clip_seconds = _validate_seconds(min_clip_seconds, "minimum clip duration")
    max_clip_seconds = _validate_seconds(max_clip_seconds, "maximum clip duration")
    if not 0 < threshold < 1:
        raise SegmentationError("threshold must be between zero and one")
    if not MIN_CLIP_SECONDS <= min_clip_seconds <= max_clip_seconds:
        raise SegmentationError("clip duration bounds are invalid")
    if max_clip_seconds > MAX_CLIP_SECONDS:
        raise SegmentationError(
            f"maximum clip duration must not exceed {MAX_CLIP_SECONDS} seconds"
        )
    for value, label in (
        (min_silence_ms, "minimum silence"),
        (speech_pad_ms, "speech padding"),
        (merge_gap_ms, "merge gap"),
    ):
        if type(value) is not int or value < 0 or value > 10_000:
            raise SegmentationError(f"{label} must be an integer from 0 to 10000 ms")

    binding = _bind_wav(audio_path)
    output = _prepare_output(output_path)
    if output == binding["path"]:
        raise SegmentationError("output must not overwrite the source audio")
    pcm = _read_pcm(binding)
    detector = vad or SileroV5VAD(
        threshold=threshold,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        model_path=model_path,
    )
    spans = await _detect_raw_spans(
        pcm, frame_count=binding["frame_count"], vad=detector
    )
    spans = _merge_spans(
        spans, max_gap_samples=round(merge_gap_ms * SAMPLE_RATE / 1000)
    )
    spans = _split_and_filter_spans(
        spans,
        min_samples=round(min_clip_seconds * SAMPLE_RATE),
        max_samples=round(max_clip_seconds * SAMPLE_RATE),
    )
    if not spans:
        raise SegmentationError("Silero did not find any eligible speech ranges")

    current_digest, current_size = _sha256(binding["path"])
    if current_digest != binding["sha256"] or current_size != binding["size_bytes"]:
        raise SegmentationError("source audio changed during segmentation")
    segments = [
        {
            "clip_id": f"{prefix}-{index:04d}",
            "start_seconds": round(start / SAMPLE_RATE, 3),
            "end_seconds": round(end / SAMPLE_RATE, 3),
        }
        for index, (start, end) in enumerate(spans, start=1)
    ]
    _write_atomic(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "source_sha256": binding["sha256"],
            "segments": segments,
        },
    )
    total_seconds = sum(end - start for start, end in spans) / SAMPLE_RATE
    print(
        f"wrote {len(segments)} candidate ranges ({total_seconds:.1f}s) to {output}",
        flush=True,
    )
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clip-prefix", required=True)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--min-silence-ms", type=int, default=550)
    parser.add_argument("--speech-pad-ms", type=int, default=96)
    parser.add_argument("--merge-gap-ms", type=int, default=800)
    parser.add_argument("--min-clip-seconds", type=float, default=MIN_CLIP_SECONDS)
    parser.add_argument("--max-clip-seconds", type=float, default=MAX_CLIP_SECONDS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        asyncio.run(
            segment_audio(
                audio_path=args.audio,
                output_path=args.output,
                clip_prefix=args.clip_prefix,
                model_path=args.model_path,
                threshold=args.threshold,
                min_silence_ms=args.min_silence_ms,
                speech_pad_ms=args.speech_pad_ms,
                merge_gap_ms=args.merge_gap_ms,
                min_clip_seconds=args.min_clip_seconds,
                max_clip_seconds=args.max_clip_seconds,
            )
        )
    except (OSError, SegmentationError) as exc:
        raise SystemExit(f"Silero segmentation failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
