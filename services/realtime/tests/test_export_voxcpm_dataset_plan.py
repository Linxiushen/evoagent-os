from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_voxcpm_dataset_plan.py"
SPEC = importlib.util.spec_from_file_location("export_voxcpm_dataset_plan", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
PREPARE_SCRIPT = ROOT / "scripts" / "prepare_voxcpm_dataset.py"
PREPARE_SPEC = importlib.util.spec_from_file_location(
    "prepare_voxcpm_dataset_for_export_tests", PREPARE_SCRIPT
)
PREPARE = importlib.util.module_from_spec(PREPARE_SPEC)
assert PREPARE_SPEC.loader is not None
sys.modules[PREPARE_SPEC.name] = PREPARE
PREPARE_SPEC.loader.exec_module(PREPARE)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_wav(path: Path, seconds: float) -> None:
    frames = round(16_000 * seconds)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x20\x00" * frames)


def _input_binding(role: str, path: Path) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path.resolve()),
        "sha256": _digest(path),
        "size_bytes": path.stat().st_size,
    }


def _review_pair(
    root: Path,
    authorization: Path,
    name: str,
    clip_ids: tuple[str, ...],
) -> tuple[Path, Path, Path]:
    source = root / f"{name}.wav"
    _write_wav(source, 3.0 * len(clip_ids))
    qwen = root / f"{name}-qwen.json"
    ocr = root / f"{name}-ocr.json"
    _write_json(qwen, {"evidence": name})
    _write_json(ocr, {"evidence": name})

    queue_root = root / f"{name}-queue"
    clips_root = queue_root / "clips"
    clips_root.mkdir(parents=True)
    clips = []
    for index, clip_id in enumerate(clip_ids):
        clip_audio = clips_root / f"{clip_id}.wav"
        _write_wav(clip_audio, 3.0)
        clips.append(
            {
                "clip_id": clip_id,
                "audio_path": f"clips/{clip_id}.wav",
                "audio_sha256": _digest(clip_audio),
                "audio_size_bytes": clip_audio.stat().st_size,
                "start_seconds": float(index * 3),
                "end_seconds": float((index + 1) * 3),
                "duration_seconds": 3.0,
                "candidate_transcript": f"Machine transcript {clip_id}.",
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
        )
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
            _input_binding("qwen_ranges", qwen),
            _input_binding("burned_subtitle_ocr", ocr),
            _input_binding("source_audio", source),
            _input_binding("authorization_record", authorization),
        ],
        "source_audio": {
            "container": "wav",
            "codec": "pcm_s16le",
            "channels": 1,
            "sample_width_bytes": 2,
            "sample_rate": 16_000,
            "frames": 48_000 * len(clip_ids),
            "duration_seconds": 3.0 * len(clip_ids),
        },
        "clips": clips,
    }
    queue_path = queue_root / "review-queue.json"
    _write_json(queue_path, queue)

    review = MODULE._review_module()
    snapshot = review.QueueSnapshot.load(queue_path)
    decisions_path = root / f"{name}-decisions.json"
    store = review.DecisionStore(decisions_path, snapshot)
    for clip_id in clip_ids:
        store.record(
            {
                "clip_id": clip_id,
                "corrected_text": f"Reviewed transcript {clip_id}.",
                "target_speaker_only": True,
                "transcript_verified": True,
                "no_third_party_speech": True,
                "no_background_music": True,
                "approved": True,
            }
        )
    return queue_path, decisions_path, source


def _export(
    tmp_path: Path,
    pairs: list[tuple[Path, Path]],
    authorization: Path,
    **overrides,
) -> Path:
    arguments = {
        "review_pairs": pairs,
        "authorization_path": authorization,
        "authorization_status": "user-attested-local-collection-and-training",
        "dataset_id": "speaker-v1",
        "subject_id": "speaker",
        "output_path": tmp_path / "dataset-plan.json",
        "validation_clip_ids": [],
    }
    arguments.update(overrides)
    return MODULE.export_dataset_plan(**arguments)


def _reject_decision(decisions_path: Path, clip_id: str) -> None:
    payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    decision = next(item for item in payload["decisions"] if item["clip_id"] == clip_id)
    decision.update(
        {
            "corrected_text": "",
            "target_speaker_only": False,
            "transcript_verified": False,
            "no_third_party_speech": False,
            "no_background_music": False,
            "approved": False,
            "gates_satisfied": False,
        }
    )
    _write_json(decisions_path, payload)


def test_exports_multiple_sources_with_immutable_review_bindings(tmp_path):
    authorization = tmp_path / "authorization.json"
    _write_json(
        authorization,
        {
            "record_id": "auth-1",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )
    first = _review_pair(tmp_path, authorization, "source-a", ("a-001", "a-002"))
    second = _review_pair(tmp_path, authorization, "source-b", ("b-001",))

    plan_path = _export(
        tmp_path,
        [(first[0], first[1]), (second[0], second[1])],
        authorization,
        validation_clip_ids=["b-001"],
    )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert [source["source_id"] for source in plan["sources"]] == [
        "source-001",
        "source-002",
    ]
    assert [clip["clip_id"] for clip in plan["clips"]] == [
        "a-001",
        "a-002",
        "b-001",
    ]
    assert plan["clips"][-1]["split"] == "validation"
    assert plan["authorization"]["third_party_model_processing"] is False
    assert len(plan["review_evidence"]) == 2
    for evidence in plan["review_evidence"]:
        assert set(evidence) == {
            "source_id",
            "queue",
            "decisions",
            "approved_clips",
        }
        for role in ("queue", "decisions"):
            assert set(evidence[role]) == {"path", "sha256", "size_bytes"}
            path = Path(evidence[role]["path"])
            assert evidence[role]["sha256"] == _digest(path)
            assert evidence[role]["size_bytes"] == path.stat().st_size


def test_exports_only_approved_subset_and_rejects_plan_insertion(tmp_path):
    authorization = tmp_path / "authorization.json"
    _write_json(
        authorization,
        {
            "record_id": "auth-1",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )
    queue, decisions, _ = _review_pair(
        tmp_path, authorization, "source", ("clip-001", "clip-002")
    )
    _reject_decision(decisions, "clip-002")

    plan_path = _export(tmp_path, [(queue, decisions)], authorization)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert [clip["clip_id"] for clip in plan["clips"]] == ["clip-001"]
    assert [
        clip["clip_id"] for clip in plan["review_evidence"][0]["approved_clips"]
    ] == ["clip-001"]

    plan["clips"].append(
        {
            "clip_id": "clip-002",
            "source_id": "source-001",
            "start_seconds": 3.0,
            "end_seconds": 6.0,
            "text": "Unapproved insertion.",
            "split": "train",
            "review": {
                "approved_for_training": True,
                "target_speaker_only": True,
                "transcript_verified": True,
                "third_party_speech": False,
                "background_music": False,
            },
        }
    )
    _write_json(plan_path, plan)
    with pytest.raises(PREPARE.DatasetPlanError, match="without review evidence"):
        PREPARE.load_and_validate_plan(plan_path)


def test_rejects_changed_review_audio(tmp_path):
    authorization = tmp_path / "authorization.json"
    _write_json(
        authorization,
        {
            "record_id": "auth-1",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )

    queue, decisions, _ = _review_pair(tmp_path, authorization, "other", ("other-001",))
    reviewed_audio = queue.parent / "clips" / "other-001.wav"
    reviewed_audio.write_bytes(reviewed_audio.read_bytes() + b"changed")
    with pytest.raises(MODULE.ReviewExportError, match="binding does not match"):
        _export(tmp_path, [(queue, decisions)], authorization)


def test_rejects_wrong_decisions_binding_and_inconsistent_authorization(tmp_path):
    authorization = tmp_path / "authorization.json"
    other_authorization = tmp_path / "other-authorization.json"
    _write_json(
        authorization,
        {
            "record_id": "auth-1",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )
    _write_json(
        other_authorization,
        {
            "record_id": "auth-2",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )
    first = _review_pair(tmp_path, authorization, "first", ("first-001",))
    payload = json.loads(first[1].read_text(encoding="utf-8"))
    payload["queue"]["sha256"] = "0" * 64
    _write_json(first[1], payload)
    with pytest.raises(MODULE.ReviewExportError, match="another queue"):
        _export(tmp_path, [(first[0], first[1])], authorization)

    second = _review_pair(tmp_path, other_authorization, "second", ("second-001",))
    with pytest.raises(MODULE.ReviewExportError, match="same supplied authorization"):
        _export(tmp_path, [(second[0], second[1])], authorization)


def test_rejects_global_clip_collision_and_preserves_existing_output(tmp_path):
    authorization = tmp_path / "authorization.json"
    _write_json(
        authorization,
        {
            "record_id": "auth-1",
            "decision": "approved",
            "status": "user-attested-local-collection-and-training",
        },
    )
    first = _review_pair(tmp_path, authorization, "first", ("clip-001",))
    second = _review_pair(tmp_path, authorization, "second", ("CLIP-001",))
    with pytest.raises(MODULE.ReviewExportError, match="filename collision"):
        _export(
            tmp_path,
            [(first[0], first[1]), (second[0], second[1])],
            authorization,
        )

    output = tmp_path / "existing-plan.json"
    output.write_text("preserve", encoding="utf-8")
    with pytest.raises(MODULE.ReviewExportError, match="already exists"):
        _export(
            tmp_path,
            [(first[0], first[1])],
            authorization,
            output_path=output,
        )
    assert output.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    ("payload", "requested_status", "message"),
    [
        (
            {"record_id": "auth-1", "decision": "approved"},
            "user-attested-local-collection-and-training",
            "record status",
        ),
        (
            {
                "record_id": "auth-1",
                "decision": "approved",
                "status": "unknown",
            },
            "user-attested-local-collection-and-training",
            "record status",
        ),
        (
            {
                "record_id": "auth-1",
                "decision": "approved",
                "status": "user-attested-local-collection-and-training",
            },
            "operator-verified",
            "does not match",
        ),
    ],
)
def test_rejects_missing_unknown_or_conflicting_authorization_status(
    tmp_path, payload, requested_status, message
):
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, payload)
    pair = _review_pair(tmp_path, authorization, "source", ("clip-001",))

    with pytest.raises(MODULE.ReviewExportError, match=message):
        _export(
            tmp_path,
            [(pair[0], pair[1])],
            authorization,
            authorization_status=requested_status,
        )


def test_cli_help_lists_review_pair_contract():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert "--review-pair QUEUE DECISIONS" in result.stdout
    assert "--authorization-status" in result.stdout
