from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import wave
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_voxcpm_dataset.py"
SPEC = importlib.util.spec_from_file_location("prepare_voxcpm_dataset", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wav(path: Path, seconds: float = 6.0) -> None:
    frames = int(16_000 * seconds)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x01\x00" * frames)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _binding(role: str, path: Path) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path.resolve()),
        "sha256": _hash(path),
        "size_bytes": path.stat().st_size,
    }


def _plan(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.wav"
    authorization = tmp_path / "authorization.json"
    _write_wav(source)
    _write_json(
        authorization,
        {
            "record_id": "auth-1",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )
    qwen = tmp_path / "qwen.json"
    ocr = tmp_path / "ocr.json"
    _write_json(qwen, {"source": "fixture"})
    _write_json(ocr, {"source": "fixture"})
    queue_root = tmp_path / "queue"
    clips_root = queue_root / "clips"
    clips_root.mkdir(parents=True)
    reviewed_audio = clips_root / "clip-001.wav"
    _write_wav(reviewed_audio, 3.0)
    queue = {
        "schema_version": 1,
        "kind": "echoweave-voxcpm2-manual-review-queue",
        "review_only": True,
        "training_ready": False,
        "ready_for_local_training": False,
        "runtime_promotion_allowed": False,
        "human_review": False,
        "approved": False,
        "approved_for_training": False,
        "transcript_verified": False,
        "review_evidence": {
            "kind": "echoweave-burned-subtitle-review-evidence",
            "ocr_performed": True,
            "reason_code": None,
        },
        "inputs": [
            _binding("qwen_ranges", qwen),
            _binding("burned_subtitle_ocr", ocr),
            _binding("source_audio", source),
            _binding("authorization_record", authorization),
        ],
        "source_audio": {
            "container": "wav",
            "codec": "pcm_s16le",
            "channels": 1,
            "sample_width_bytes": 2,
            "sample_rate": 16_000,
            "frames": 96_000,
            "duration_seconds": 6.0,
        },
        "clips": [
            {
                "clip_id": "clip-001",
                "audio_path": "clips/clip-001.wav",
                "audio_sha256": _hash(reviewed_audio),
                "audio_size_bytes": reviewed_audio.stat().st_size,
                "start_seconds": 0.0,
                "end_seconds": 3.0,
                "duration_seconds": 3.0,
                "candidate_transcript": "Machine transcript.",
                "ocr_evidence": {
                    "subtitle_text": "",
                    "similarity": 0.0,
                    "caption_count": 0,
                },
                "human_review": False,
                "approved": False,
                "approved_for_training": False,
                "transcript_verified": False,
            }
        ],
    }
    queue_path = queue_root / "review-queue.json"
    _write_json(queue_path, queue)

    exporter = MODULE._review_export_module()
    review = exporter._review_module()
    snapshot = review.QueueSnapshot.load(queue_path)
    decisions_path = tmp_path / "decisions.json"
    store = review.DecisionStore(decisions_path, snapshot)
    store.record(
        {
            "clip_id": "clip-001",
            "corrected_text": "Reviewed transcript.",
            "target_speaker_only": True,
            "transcript_verified": True,
            "no_third_party_speech": True,
            "no_background_music": True,
            "approved": True,
        }
    )
    plan_path = tmp_path / "plan.json"
    exporter.export_dataset_plan(
        review_pairs=[(queue_path, decisions_path)],
        authorization_path=authorization,
        authorization_status="user-attested-local-collection-and-training",
        dataset_id="speaker-v1",
        subject_id="speaker",
        output_path=plan_path,
    )
    return plan_path


def test_plan_requires_reviewed_single_speaker_audio(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["clips"][0]["review"]["third_party_speech"] = True
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match="third_party_speech"):
        MODULE.load_and_validate_plan(plan_path)


def test_plan_binds_source_and_authorization_hashes(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["sources"][0]["audio_sha256"] = "0" * 64
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match="does not match"):
        MODULE.load_and_validate_plan(plan_path)


def test_plan_rejects_symbolic_link_source(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    source = tmp_path / payload["sources"][0]["audio_path"]
    link = tmp_path / "source-link.wav"
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    payload["sources"][0]["audio_path"] = link.name
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match="symbolic link"):
        MODULE.load_and_validate_plan(plan_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", [], "authorization status"),
        ("third_party_model_processing", "false", "must be a boolean"),
    ],
)
def test_plan_rejects_malformed_authorization_values(tmp_path, field, value, message):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["authorization"][field] = value
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match=message):
        MODULE.load_and_validate_plan(plan_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_id", [], "source_id is invalid"),
        ("split", [], "split must be train or validation"),
        ("start_seconds", True, "invalid timestamps"),
        ("end_seconds", "3.0", "invalid timestamps"),
        ("end_seconds", 10**400, "invalid timestamps"),
    ],
)
def test_plan_rejects_malformed_clip_values(tmp_path, field, value, message):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["clips"][0][field] = value
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match=message):
        MODULE.load_and_validate_plan(plan_path)


def test_plan_rejects_case_insensitive_output_collisions(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    second = dict(payload["clips"][0])
    second["clip_id"] = "CLIP-001"
    second["start_seconds"] = 3.0
    second["end_seconds"] = 6.0
    second["review"] = dict(payload["clips"][0]["review"])
    payload["clips"].append(second)
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match="filename collision"):
        MODULE.load_and_validate_plan(plan_path)


def test_plan_rejects_overlapping_ranges(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    second = dict(payload["clips"][0])
    second["clip_id"] = "clip-002"
    second["start_seconds"] = 1.5
    second["end_seconds"] = 4.5
    second["review"] = dict(payload["clips"][0]["review"])
    payload["clips"].append(second)
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.DatasetPlanError, match="overlapping"):
        MODULE.load_and_validate_plan(plan_path)


def test_wav_validation_rejects_silence(tmp_path):
    wav_path = tmp_path / "silent.wav"
    with wave.open(str(wav_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 16_000)

    with pytest.raises(MODULE.DatasetPlanError, match="silent"):
        MODULE._wav_metadata(wav_path, 1.0)


def test_plan_requires_review_evidence_and_exact_reviewed_text(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload.pop("review_evidence")
    _write_json(plan_path, payload)
    with pytest.raises(MODULE.DatasetPlanError, match="review_evidence"):
        MODULE.load_and_validate_plan(plan_path)

    plan_path.unlink()
    plan_path = _plan(tmp_path / "second")
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["clips"][0]["text"] = "Tampered transcript."
    _write_json(plan_path, payload)
    with pytest.raises(MODULE.DatasetPlanError, match="corrected text changed"):
        MODULE.load_and_validate_plan(plan_path)


def test_plan_rejects_changed_decisions_or_evidence_binding(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    decisions = Path(payload["review_evidence"][0]["decisions"]["path"])
    decisions.write_bytes(decisions.read_bytes() + b"changed")
    with pytest.raises(MODULE.DatasetPlanError, match="binding does not match"):
        MODULE.load_and_validate_plan(plan_path)

    other = tmp_path / "other"
    other.mkdir()
    plan_path = _plan(other)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["review_evidence"][0]["queue"]["sha256"] = "0" * 64
    _write_json(plan_path, payload)
    with pytest.raises(MODULE.DatasetPlanError, match="binding does not match"):
        MODULE.load_and_validate_plan(plan_path)


def test_plan_forbids_third_party_processing(tmp_path):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["authorization"]["third_party_model_processing"] = True
    _write_json(plan_path, payload)

    with pytest.raises(MODULE.DatasetPlanError, match="must be false"):
        MODULE.load_and_validate_plan(plan_path)


def test_build_rechecks_decisions_before_publishing_dataset(tmp_path, monkeypatch):
    plan_path = _plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    decisions = Path(payload["review_evidence"][0]["decisions"]["path"])
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"fixture")
    output = tmp_path / "dataset"

    monkeypatch.setattr(MODULE, "MIN_TRAIN_SECONDS", 3.0)
    monkeypatch.setattr(MODULE, "_ffmpeg_version", lambda _path: "ffmpeg fixture")

    def extract_and_mutate(_ffmpeg, source, destination, start, end):
        shutil.copyfile(source, destination)
        with wave.open(str(destination), "rb") as stream:
            frames = stream.readframes(round((end - start) * 16_000))
        with wave.open(str(destination), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16_000)
            stream.writeframes(frames)
        decisions.write_bytes(decisions.read_bytes() + b"changed")
        return ["ffmpeg-fixture"]

    monkeypatch.setattr(MODULE, "_extract_clip", extract_and_mutate)
    with pytest.raises(MODULE.DatasetPlanError, match="review evidence changed"):
        MODULE.build_dataset(plan_path, output, ffmpeg)
    assert not output.exists()
