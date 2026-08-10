from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ocr_burned_subtitles.py"
SPEC = importlib.util.spec_from_file_location("ocr_burned_subtitles", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_merge_observation_keeps_best_duplicate_text():
    captions = []
    MODULE._merge_observation(
        captions,
        timestamp=1.0,
        text="属于零售行亚啊",
        confidence=0.75,
        frame_seconds=0.5,
        duplicate_similarity=0.8,
    )
    MODULE._merge_observation(
        captions,
        timestamp=1.5,
        text="属于零售行业啊",
        confidence=0.99,
        frame_seconds=0.5,
        duplicate_similarity=0.8,
    )

    assert captions == [
        {
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text": "属于零售行业啊",
            "confidence": 0.99,
            "observations": 2,
        }
    ]


def test_alignment_reports_asr_subtitle_similarity(tmp_path):
    segments = tmp_path / "segments.json"
    segments.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "clip_id": "clip-1",
                        "start_seconds": 1.0,
                        "end_seconds": 3.0,
                        "text": "因为这个自媒体啊",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    captions = [
        {
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text": "因为这个自媒体啊",
        }
    ]

    aligned = MODULE._align_segments(segments, captions, tolerance=0.1)

    assert aligned[0]["subtitle_text"] == "因为这个自媒体啊"
    assert aligned[0]["similarity"] == 1.0


def test_inside_ranges_honors_alignment_tolerance():
    ranges = [(10.0, 12.0), (20.0, 22.0)]

    assert MODULE._inside_ranges(9.5, ranges, tolerance=0.5)
    assert MODULE._inside_ranges(21.0, ranges, tolerance=0.5)
    assert not MODULE._inside_ranges(15.0, ranges, tolerance=0.5)


def test_segment_sampling_is_even_and_frame_bounded():
    samples = MODULE._sample_timestamps([(10.0, 14.0)], samples_per_segment=3)

    assert samples == [11.0, 12.0, 13.0]
    assert MODULE._near_sample(11.4, samples, frame_seconds=1.0)
    assert not MODULE._near_sample(10.4, samples, frame_seconds=1.0)
