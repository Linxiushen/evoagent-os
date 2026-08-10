"""Extract and time-align burned subtitles for private transcript review.

This tool never marks a transcript as human-reviewed. Its JSON output is review
evidence that can be compared with independent ASR results before a training
dataset plan is approved.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
COMPARISON_TEXT = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")


class SubtitleExtractionError(RuntimeError):
    """Raised when local subtitle extraction cannot be completed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _comparison_text(value: str) -> str:
    return COMPARISON_TEXT.sub("", value).lower()


def _similarity(left: str, right: str) -> float:
    left = _comparison_text(left)
    right = _comparison_text(right)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(a=left, b=right, autojunk=False).ratio()


def _merge_observation(
    captions: list[dict[str, Any]],
    *,
    timestamp: float,
    text: str,
    confidence: float,
    frame_seconds: float,
    duplicate_similarity: float,
) -> None:
    cleaned = "".join(text.split())
    if not cleaned:
        return
    if captions and _similarity(captions[-1]["text"], cleaned) >= duplicate_similarity:
        current = captions[-1]
        current["end_seconds"] = round(timestamp + frame_seconds, 3)
        current["observations"] += 1
        if confidence > current["confidence"]:
            current["text"] = cleaned
            current["confidence"] = round(confidence, 6)
        return
    captions.append(
        {
            "start_seconds": round(timestamp, 3),
            "end_seconds": round(timestamp + frame_seconds, 3),
            "text": cleaned,
            "confidence": round(confidence, 6),
            "observations": 1,
        }
    )


def _align_segments(
    segments_path: Path, captions: list[dict[str, Any]], tolerance: float
) -> list[dict[str, Any]]:
    payload = json.loads(segments_path.read_text(encoding="utf-8"))
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise SubtitleExtractionError("segments JSON must contain a segments array")
    aligned = []
    for index, segment in enumerate(raw_segments):
        try:
            start = float(segment.get("start_seconds", segment.get("start")))
            end = float(segment.get("end_seconds", segment.get("end")))
            transcript = str(segment["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise SubtitleExtractionError(f"invalid segment at index {index}") from exc
        matched = []
        for caption in captions:
            midpoint = (caption["start_seconds"] + caption["end_seconds"]) / 2
            if start - tolerance <= midpoint <= end + tolerance:
                matched.append(caption)
        subtitle_text = "".join(item["text"] for item in matched)
        aligned.append(
            {
                "clip_id": segment.get("clip_id", f"segment-{index + 1:04d}"),
                "start_seconds": start,
                "end_seconds": end,
                "asr_text": transcript,
                "subtitle_text": subtitle_text,
                "similarity": round(_similarity(transcript, subtitle_text), 6),
                "caption_count": len(matched),
                "caption_indexes": [captions.index(item) for item in matched],
            }
        )
    return aligned


def _load_segment_ranges(segments_path: Path) -> list[tuple[float, float]]:
    payload = json.loads(segments_path.read_text(encoding="utf-8"))
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise SubtitleExtractionError("segments JSON must contain a segments array")
    ranges = []
    for index, segment in enumerate(raw_segments):
        try:
            start = float(segment.get("start_seconds", segment.get("start")))
            end = float(segment.get("end_seconds", segment.get("end")))
        except (TypeError, ValueError) as exc:
            raise SubtitleExtractionError(f"invalid segment at index {index}") from exc
        if start < 0 or end <= start:
            raise SubtitleExtractionError(f"invalid segment range at index {index}")
        ranges.append((start, end))
    return ranges


def _inside_ranges(
    timestamp: float, ranges: list[tuple[float, float]], tolerance: float
) -> bool:
    return any(
        start - tolerance <= timestamp <= end + tolerance for start, end in ranges
    )


def _sample_timestamps(
    ranges: list[tuple[float, float]], samples_per_segment: int
) -> list[float]:
    if samples_per_segment <= 0:
        return []
    return [
        start + ((end - start) * sample / (samples_per_segment + 1))
        for start, end in ranges
        for sample in range(1, samples_per_segment + 1)
    ]


def _near_sample(timestamp: float, samples: list[float], frame_seconds: float) -> bool:
    return any(abs(timestamp - sample) <= frame_seconds / 2 for sample in samples)


def extract_subtitles(
    *,
    input_path: Path,
    output_path: Path,
    ffmpeg: Path,
    segments_path: Path | None,
    fps: float,
    crop_height: int,
    scale_width: int,
    confidence_threshold: float,
    duplicate_similarity: float,
    alignment_tolerance: float,
    samples_per_segment: int,
) -> Path:
    if fps <= 0 or crop_height <= 0 or scale_width <= 0:
        raise SubtitleExtractionError(
            "fps, crop-height and scale-width must be positive"
        )
    input_path = input_path.resolve(strict=True)
    ffmpeg = ffmpeg.resolve(strict=True)
    output_path = output_path.resolve()
    if output_path.exists():
        raise SubtitleExtractionError(f"output already exists: {output_path}")
    if segments_path is not None:
        segments_path = segments_path.resolve(strict=True)
    segment_ranges = (
        _load_segment_ranges(segments_path) if segments_path is not None else []
    )
    if samples_per_segment < 0:
        raise SubtitleExtractionError("samples-per-segment cannot be negative")
    if samples_per_segment and not segment_ranges:
        raise SubtitleExtractionError("samples-per-segment requires --segments")
    sample_timestamps = _sample_timestamps(segment_ranges, samples_per_segment)

    try:
        import cv2
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise SubtitleExtractionError(
            "install rapidocr_onnxruntime and opencv-python in the curation environment"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="echoweave-subtitle-ocr-") as temp:
        frame_pattern = Path(temp) / "%08d.jpg"
        filtergraph = (
            f"fps={fps},crop=iw:{crop_height}:0:ih-{crop_height},"
            f"scale={scale_width}:-1:flags=lanczos"
        )
        command = [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-an",
            "-vf",
            filtergraph,
            "-q:v",
            "3",
            str(frame_pattern),
        ]
        result = subprocess.run(command, check=False, capture_output=True, timeout=900)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
            raise SubtitleExtractionError(f"ffmpeg frame extraction failed: {detail}")

        frames = sorted(Path(temp).glob("*.jpg"))
        if not frames:
            raise SubtitleExtractionError("ffmpeg produced no subtitle review frames")
        engine = RapidOCR()
        captions: list[dict[str, Any]] = []
        frame_seconds = 1.0 / fps
        analyzed_frames = 0
        for frame_index, frame in enumerate(frames):
            timestamp = frame_index * frame_seconds
            if sample_timestamps and not _near_sample(
                timestamp, sample_timestamps, frame_seconds
            ):
                continue
            if (
                not sample_timestamps
                and segment_ranges
                and not _inside_ranges(timestamp, segment_ranges, alignment_tolerance)
            ):
                continue
            image = cv2.imread(str(frame))
            if image is None:
                raise SubtitleExtractionError(f"could not decode frame: {frame.name}")
            result, _ = engine(image)
            analyzed_frames += 1
            if analyzed_frames % 50 == 0:
                print(
                    f"OCR frames: {analyzed_frames}/{len(frames)}",
                    file=sys.stderr,
                    flush=True,
                )
            observations = [
                (str(item[1]), float(item[2]))
                for item in (result or [])
                if float(item[2]) >= confidence_threshold
            ]
            if not observations:
                continue
            text = "".join(item[0] for item in observations)
            confidence = sum(item[1] for item in observations) / len(observations)
            _merge_observation(
                captions,
                timestamp=timestamp,
                text=text,
                confidence=confidence,
                frame_seconds=frame_seconds,
                duplicate_similarity=duplicate_similarity,
            )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "echoweave-burned-subtitle-review-evidence",
        "input": {
            "path": str(input_path),
            "sha256": _sha256(input_path),
        },
        "method": {
            "engine": "rapidocr_onnxruntime",
            "fps": fps,
            "crop_height": crop_height,
            "scale_width": scale_width,
            "confidence_threshold": confidence_threshold,
            "duplicate_similarity": duplicate_similarity,
            "speech_segment_filter": bool(segment_ranges),
            "samples_per_segment": samples_per_segment,
            "ffmpeg_filtergraph": filtergraph,
        },
        "captions": captions,
        "limitations": [
            "OCR evidence is not human transcript approval.",
            "Frame sampling can miss subtitles displayed for less than one frame interval.",
            "Burned subtitles may intentionally differ from spoken fillers or pronunciation.",
        ],
    }
    if segments_path is not None:
        payload["segments"] = {
            "path": str(segments_path),
            "sha256": _sha256(segments_path),
        }
        payload["alignment"] = _align_segments(
            segments_path, captions, alignment_tolerance
        )
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--segments", type=Path)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--crop-height", type=int, default=180)
    parser.add_argument("--scale-width", type=int, default=1920)
    parser.add_argument("--confidence-threshold", type=float, default=0.72)
    parser.add_argument("--duplicate-similarity", type=float, default=0.84)
    parser.add_argument("--alignment-tolerance", type=float, default=0.75)
    parser.add_argument("--samples-per-segment", type=int, default=0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        output = extract_subtitles(
            input_path=args.input,
            output_path=args.output,
            ffmpeg=args.ffmpeg,
            segments_path=args.segments,
            fps=args.fps,
            crop_height=args.crop_height,
            scale_width=args.scale_width,
            confidence_threshold=args.confidence_threshold,
            duplicate_similarity=args.duplicate_similarity,
            alignment_tolerance=args.alignment_tolerance,
            samples_per_segment=args.samples_per_segment,
        )
    except (OSError, ValueError, json.JSONDecodeError, SubtitleExtractionError) as exc:
        raise SystemExit(f"subtitle extraction failed: {exc}") from exc
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
