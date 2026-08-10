from __future__ import annotations

import hashlib
import importlib.util
import json
import wave
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "prepare_voxcpm_review_queue.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_voxcpm_review_queue", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_wav(path: Path, seconds: float = 9.0) -> None:
    frames = int(MODULE.SAMPLE_RATE * seconds)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(MODULE.SAMPLE_RATE)
        stream.writeframes(b"\x10\x00" * frames)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    audio = tmp_path / "source.wav"
    ranges = tmp_path / "qwen-ranges.json"
    ocr = tmp_path / "burned-subtitle-ocr.json"
    authorization = tmp_path / "authorization.json"
    ffmpeg = tmp_path / "ffmpeg-fixture"
    _write_wav(audio)
    segments = [
        {
            "clip_id": f"clip-{index:03d}",
            "start_seconds": float((index - 1) * 3),
            "end_seconds": float(index * 3),
            "text": f"Machine transcript {index}.",
        }
        for index in range(1, 4)
    ]
    _write_json(
        ranges,
        {
            "schema_version": 1,
            "source_path": "evidence/original-source.wav",
            "source_sha256": _digest(audio),
            "segments": segments,
        },
    )
    _write_json(
        ocr,
        {
            "schema_version": 1,
            "kind": "echoweave-burned-subtitle-review-evidence",
            "segments": {"path": str(ranges), "sha256": _digest(ranges)},
            "alignment": [
                {
                    "clip_id": item["clip_id"],
                    "start_seconds": item["start_seconds"],
                    "end_seconds": item["end_seconds"],
                    "asr_text": item["text"],
                    "subtitle_text": f"Subtitle {index}.",
                    "similarity": 0.5,
                    "caption_count": 1,
                }
                for index, item in enumerate(segments, start=1)
            ],
        },
    )
    _write_json(authorization, {"record_id": "auth-fixture", "status": "attested"})
    ffmpeg.write_bytes(b"synthetic ffmpeg fixture")
    return {
        "audio": audio,
        "ranges": ranges,
        "ocr": ocr,
        "authorization": authorization,
        "ffmpeg": ffmpeg,
        "output": tmp_path / "review-queue",
    }


def _fake_extract(
    _ffmpeg: Path,
    source: Path,
    destination: Path,
    start: float,
    duration: float,
) -> list[str]:
    with wave.open(str(source), "rb") as input_stream:
        input_stream.setpos(round(start * MODULE.SAMPLE_RATE))
        frames = input_stream.readframes(round(duration * MODULE.SAMPLE_RATE))
    with wave.open(str(destination), "wb") as output_stream:
        output_stream.setnchannels(1)
        output_stream.setsampwidth(2)
        output_stream.setframerate(MODULE.SAMPLE_RATE)
        output_stream.writeframes(frames)
    return [
        "fake-ffmpeg",
        "-i",
        str(source),
        "-ss",
        str(start),
        "-t",
        str(duration),
        str(destination),
    ]


def _build(paths: dict[str, Path], monkeypatch, **overrides) -> Path:
    monkeypatch.setattr(MODULE, "_ffmpeg_version", lambda _path: "ffmpeg fixture")
    monkeypatch.setattr(MODULE, "_extract_clip", _fake_extract)
    arguments = {
        "ranges_path": paths["ranges"],
        "ocr_path": paths["ocr"],
        "audio_path": paths["audio"],
        "authorization_path": paths["authorization"],
        "output_dir": paths["output"],
        "ffmpeg": paths["ffmpeg"],
    }
    arguments.update(overrides)
    return MODULE.build_review_queue(**arguments)


def _assert_review_flags_are_false(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "human_review",
                "approved",
                "approved_for_training",
                "transcript_verified",
            }:
                assert child is False
            _assert_review_flags_are_false(child)
    elif isinstance(value, list):
        for child in value:
            _assert_review_flags_are_false(child)


def test_builds_unapproved_review_queue_with_explicit_exclusion(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)

    manifest_path = _build(paths, monkeypatch, exclude_clip_ids=["clip-002"])

    manifest_text = manifest_path.read_text(encoding="utf-8")
    payload = json.loads(manifest_text)
    assert payload["kind"] == "echoweave-voxcpm2-manual-review-queue"
    assert payload["review_only"] is True
    assert payload["training_ready"] is False
    assert payload["ready_for_local_training"] is False
    assert payload["runtime_promotion_allowed"] is False
    assert payload["excluded_clip_ids"] == ["clip-002"]
    assert [item["clip_id"] for item in payload["clips"]] == [
        "clip-001",
        "clip-003",
    ]
    _assert_review_flags_are_false(payload)
    assert not list(paths["output"].glob("*.jsonl"))
    assert not (paths["output"] / "train.jsonl").exists()
    assert sorted(path.name for path in (paths["output"] / "clips").iterdir()) == [
        "clip-001.wav",
        "clip-003.wav",
    ]
    bindings = {item["role"]: item for item in payload["inputs"]}
    assert bindings["qwen_ranges"]["sha256"] == _digest(paths["ranges"])
    assert bindings["burned_subtitle_ocr"]["sha256"] == _digest(paths["ocr"])
    assert bindings["source_audio"]["sha256"] == _digest(paths["audio"])
    assert bindings["authorization_record"]["sha256"] == _digest(paths["authorization"])
    assert payload["qwen_source_evidence"] == {
        "source_path_claim": "evidence/original-source.wav",
        "source_sha256": _digest(paths["audio"]),
    }
    assert payload["review_evidence"] == {
        "kind": MODULE.BURNED_SUBTITLE_KIND,
        "ocr_performed": True,
        "reason_code": None,
    }
    assert "${SOURCE_AUDIO}" in manifest_text
    assert "${OUTPUT_WAV}" in manifest_text
    assert ".review-queue-" not in manifest_text


def test_builds_queue_with_explicit_audio_only_evidence(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    transcript = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    _write_json(
        paths["ocr"],
        {
            "schema_version": 1,
            "kind": MODULE.AUDIO_ONLY_KIND,
            "input": {
                "path": str(paths["audio"]),
                "sha256": _digest(paths["audio"]),
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
                "path": str(paths["ranges"]),
                "sha256": _digest(paths["ranges"]),
            },
            "alignment": [
                {
                    "clip_id": item["clip_id"],
                    "start_seconds": item["start_seconds"],
                    "end_seconds": item["end_seconds"],
                    "asr_text": item["text"],
                    "subtitle_text": "",
                    "similarity": 0.0,
                    "caption_count": 0,
                }
                for item in transcript["segments"]
            ],
        },
    )

    manifest = json.loads(_build(paths, monkeypatch).read_text(encoding="utf-8"))

    assert manifest["review_evidence"] == {
        "kind": MODULE.AUDIO_ONLY_KIND,
        "ocr_performed": False,
        "reason_code": "audio_only_no_video_stream",
    }
    assert all(
        clip["ocr_evidence"]
        == {"subtitle_text": "", "similarity": 0.0, "caption_count": 0}
        for clip in manifest["clips"]
    )
    assert any("OCR was not performed" in item for item in manifest["limitations"])


def test_rejects_existing_output_without_overwriting(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    paths["output"].mkdir()
    marker = paths["output"] / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(MODULE.ReviewQueueError, match="already exists"):
        _build(paths, monkeypatch)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_output_path_containment_rejects_parent_traversal(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(MODULE.ReviewQueueError, match="escapes staging"):
        MODULE._contained_path(staging, Path("..") / "escaped.wav")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda segments: segments[1].update(
                {"start_seconds": 2.5, "end_seconds": 5.5}
            ),
            "overlapping",
        ),
        (
            lambda segments: segments[1].update({"clip_id": "CLIP-001"}),
            "filename collision",
        ),
        (
            lambda segments: segments[0].update({"clip_id": "../escape"}),
            "clip_id is invalid",
        ),
        (
            lambda segments: segments[0].update({"end_seconds": 2.9}),
            "duration must be",
        ),
    ],
)
def test_rejects_unsafe_qwen_ranges(tmp_path, monkeypatch, mutation, message):
    paths = _fixture(tmp_path)
    payload = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    mutation(payload["segments"])
    _write_json(paths["ranges"], payload)

    with pytest.raises(MODULE.ReviewQueueError, match=message):
        _build(paths, monkeypatch)

    assert not paths["output"].exists()


def test_rejects_ocr_evidence_bound_to_different_ranges(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    payload = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    payload["segments"][0]["text"] = "A changed machine transcript."
    _write_json(paths["ranges"], payload)

    with pytest.raises(MODULE.ReviewQueueError, match="does not match"):
        _build(paths, monkeypatch)

    assert not paths["output"].exists()


def test_rejects_qwen_ranges_bound_to_different_audio(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    payload = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    payload["source_sha256"] = "0" * 64
    _write_json(paths["ranges"], payload)

    with pytest.raises(MODULE.ReviewQueueError, match="does not match"):
        _build(paths, monkeypatch)

    assert not paths["output"].exists()


def test_qwen_source_path_is_retained_but_never_resolved(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    payload = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    payload["source_path"] = "../../missing-and-untrusted.wav"
    _write_json(paths["ranges"], payload)
    ocr = json.loads(paths["ocr"].read_text(encoding="utf-8"))
    ocr["segments"]["sha256"] = _digest(paths["ranges"])
    _write_json(paths["ocr"], ocr)

    manifest_path = _build(paths, monkeypatch)

    result = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["qwen_source_evidence"]["source_path_claim"] == (
        "../../missing-and-untrusted.wav"
    )


def test_rejects_unknown_or_duplicate_exclusions(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    with pytest.raises(MODULE.ReviewQueueError, match="unknown excluded"):
        _build(paths, monkeypatch, exclude_clip_ids=["clip-999"])

    with pytest.raises(MODULE.ReviewQueueError, match="duplicate excluded"):
        _build(paths, monkeypatch, exclude_clip_ids=["clip-001", "clip-001"])


def test_detects_source_change_and_leaves_no_partial_output(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)

    def mutate_after_extract(*args, **kwargs):
        command = _fake_extract(*args, **kwargs)
        source = args[1]
        source.write_bytes(source.read_bytes() + b"changed")
        return command

    monkeypatch.setattr(MODULE, "_ffmpeg_version", lambda _path: "ffmpeg fixture")
    monkeypatch.setattr(MODULE, "_extract_clip", mutate_after_extract)
    with pytest.raises(MODULE.ReviewQueueError, match="source audio changed"):
        MODULE.build_review_queue(
            ranges_path=paths["ranges"],
            ocr_path=paths["ocr"],
            audio_path=paths["audio"],
            authorization_path=paths["authorization"],
            output_dir=paths["output"],
            ffmpeg=paths["ffmpeg"],
            exclude_clip_ids=["clip-002", "clip-003"],
        )

    assert not paths["output"].exists()
    assert not list(tmp_path.glob(".review-queue-*"))


def test_rejects_symbolic_link_input(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    link = tmp_path / "source-link.wav"
    try:
        link.symlink_to(paths["audio"])
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(MODULE.ReviewQueueError, match="symbolic links"):
        _build(paths, monkeypatch, audio_path=link)
