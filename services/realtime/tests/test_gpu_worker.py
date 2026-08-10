from __future__ import annotations

import hashlib
import json
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest

from echoweave import gpu_worker

ZERO_DIGEST = "0" * 64
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


def _json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * 16_000)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * frames)


def _artifact(path: Path, artifact_id: str, role: str, media_type: str) -> dict:
    return {
        "id": artifact_id,
        "role": role,
        "path": str(path),
        "sha256": _digest(path),
        "media_type": media_type,
    }


def _evaluator(tmp_path: Path) -> dict:
    path = tmp_path / "evaluators" / "fixture.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pinned evaluator fixture")
    return {
        "name": "fixture",
        "version": "1",
        "path": str(path),
        "artifact_sha256": _digest(path),
    }


def _authorization(tmp_path: Path, *, scopes: list[str], processors: list[str]):
    record = tmp_path / "inputs" / "authorization.json"
    authorization = {
        "record_id": "auth-001",
        "record_sha256": ZERO_DIGEST,
        "status": "approved",
        "subject_verified": True,
        "authority_verified": True,
        "approved_at": "2026-08-01T00:00:00Z",
        "valid_until": "2027-08-01T00:00:00Z",
        "scopes": scopes,
        "allowed_processors": processors,
    }
    _json(
        record,
        {
            "schema_version": 1,
            "artifact_kind": gpu_worker.AUTHORIZATION_RECORD_KIND,
            **{
                key: value
                for key, value in authorization.items()
                if key != "record_sha256"
            },
        },
    )
    authorization["record_sha256"] = _digest(record)
    return authorization, _artifact(
        record, "authorization-record", "authorization_record", "application/json"
    )


def _model(
    tmp_path: Path,
    *,
    model_id: str,
    revision: str,
    files: tuple[str, ...],
) -> dict:
    root = tmp_path / "models" / model_id.replace("/", "--")
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fixture:{model_id}:{relative}".encode())
    snapshot = tmp_path / "locks" / f"{root.name}.snapshot.json"
    gpu_worker.create_model_snapshot(
        root,
        model_id=model_id,
        revision=revision,
        output=snapshot,
    )
    return {
        "id": model_id,
        "revision": revision,
        "path": str(root),
        "snapshot": {"path": str(snapshot), "sha256": _digest(snapshot)},
    }


def _zero_job(tmp_path: Path) -> tuple[Path, dict]:
    authorization, authorization_artifact = _authorization(
        tmp_path,
        scopes=["voice_clone"],
        processors=[gpu_worker.VOXCPM_MODEL_ID],
    )
    reference = tmp_path / "inputs" / "reference.wav"
    transcript = tmp_path / "inputs" / "reference.txt"
    synthesis = tmp_path / "inputs" / "synthesis.txt"
    _wav(reference, 3.0)
    transcript.write_text("An exact synthetic fixture transcript.\n", encoding="utf-8")
    synthesis.write_text("A synthetic qualification sentence.\n", encoding="utf-8")
    model = _model(
        tmp_path,
        model_id=gpu_worker.VOXCPM_MODEL_ID,
        revision=gpu_worker.VOXCPM_MODEL_REVISION,
        files=("config.json", "model.safetensors", "audiovae.pth"),
    )
    repository = tmp_path / "source" / "VoxCPM"
    (repository / "src" / "voxcpm").mkdir(parents=True)
    job = {
        "schema_version": 1,
        "run_id": "synthetic-zero-v1",
        "mode": gpu_worker.MODE_VOX_ZERO,
        "authorization": authorization,
        "models": [model],
        "source_repositories": [
            {
                "id": gpu_worker.VOXCPM_REPO_ID,
                "revision": gpu_worker.VOXCPM_REPO_REVISION,
                "path": str(repository),
            }
        ],
        "inputs": [
            authorization_artifact,
            _artifact(reference, "reference", "voice_reference_wav", "audio/wav"),
            _artifact(
                transcript,
                "reference-transcript",
                "voice_reference_transcript",
                "text/plain",
            ),
            _artifact(synthesis, "synthesis", "synthesis_text", "text/plain"),
        ],
        "execution": {
            "gpu_index": 0,
            "seed": 42,
            "output_dir": str(tmp_path / "outputs"),
            "output_filename": "candidate.wav",
        },
    }
    manifest = tmp_path / "job.json"
    _json(manifest, job)
    return manifest, job


def test_zero_shot_job_binds_private_inputs_and_model_snapshot(tmp_path: Path):
    manifest, job = _zero_job(tmp_path)

    validated = gpu_worker.validate_job(manifest, now=NOW)

    assert validated["mode"] == gpu_worker.MODE_VOX_ZERO
    assert validated["mode_details"]["voice_reference_duration_seconds"] == 3.0
    assert validated["models"][0]["file_count"] == 3
    repeated_snapshot = tmp_path / "locks" / "repeated.snapshot.json"
    gpu_worker.create_model_snapshot(
        Path(job["models"][0]["path"]),
        model_id=gpu_worker.VOXCPM_MODEL_ID,
        revision=gpu_worker.VOXCPM_MODEL_REVISION,
        output=repeated_snapshot,
    )
    assert (
        repeated_snapshot.read_bytes()
        == Path(job["models"][0]["snapshot"]["path"]).read_bytes()
    )

    Path(job["inputs"][1]["path"]).write_bytes(b"changed")
    with pytest.raises(gpu_worker.WorkerPackError, match="SHA-256"):
        gpu_worker.validate_job(manifest, now=NOW)


def test_use_time_binding_rejects_artifact_replacement(tmp_path: Path):
    manifest, job = _zero_job(tmp_path)
    validated = gpu_worker.validate_job(manifest, now=NOW)
    Path(job["inputs"][1]["path"]).write_bytes(b"replaced after validation")

    with pytest.raises(gpu_worker.WorkerPackError, match="changed before inference"):
        gpu_worker._verify_validated_binding(validated, phase="inference")


def test_job_rejects_secret_fields_and_missing_scope(tmp_path: Path):
    _, job = _zero_job(tmp_path)
    job["api_key"] = "not-even-a-real-key"
    secret_manifest = tmp_path / "secret-job.json"
    _json(secret_manifest, job)

    with pytest.raises(gpu_worker.WorkerPackError, match="secret-like field"):
        gpu_worker.validate_job(secret_manifest, verify_files=False, now=NOW)

    job.pop("api_key")
    job["authorization"]["scopes"] = []
    missing_scope = tmp_path / "missing-scope-job.json"
    _json(missing_scope, job)
    with pytest.raises(gpu_worker.WorkerPackError, match="does not cover"):
        gpu_worker.validate_job(missing_scope, verify_files=False, now=NOW)


def test_job_rejects_pending_authorization_artifact(tmp_path: Path):
    manifest, job = _zero_job(tmp_path)
    authorization_path = Path(job["inputs"][0]["path"])
    payload = json.loads(authorization_path.read_text(encoding="utf-8"))
    payload["status"] = "pending"
    _json(authorization_path, payload)
    job["inputs"][0]["sha256"] = _digest(authorization_path)
    job["authorization"]["record_sha256"] = _digest(authorization_path)
    _json(manifest, job)

    with pytest.raises(gpu_worker.WorkerPackError, match="does not match"):
        gpu_worker.validate_job(manifest, now=NOW)


def test_authorization_record_cannot_be_broadened_by_job_fields(tmp_path: Path):
    manifest, job = _zero_job(tmp_path)
    authorization_path = Path(job["inputs"][0]["path"])
    payload = json.loads(authorization_path.read_text(encoding="utf-8"))
    payload["scopes"] = ["avatar_animation"]
    _json(authorization_path, payload)
    digest = _digest(authorization_path)
    job["inputs"][0]["sha256"] = digest
    job["authorization"]["record_sha256"] = digest
    _json(manifest, job)

    with pytest.raises(gpu_worker.WorkerPackError, match="scopes or processors"):
        gpu_worker.validate_job(manifest, now=NOW)


def test_snapshot_rejects_unmanifested_model_files(tmp_path: Path):
    manifest, job = _zero_job(tmp_path)
    model_root = Path(job["models"][0]["path"])
    (model_root / "unexpected.bin").write_bytes(b"not locked")

    with pytest.raises(gpu_worker.WorkerPackError, match="unmanifested"):
        gpu_worker.validate_job(manifest, now=NOW)


def test_lora_manifest_requires_unique_clean_five_minute_dataset(tmp_path: Path):
    artifacts = []
    samples = []
    derivations = []
    for index in range(10):
        audio = tmp_path / "training" / "clips" / f"{index:02d}.wav"
        _wav(audio, 30.0)
        artifacts.append(
            {
                "path": audio,
                "role": "voxcpm_train_audio",
                "sha256": _digest(audio),
                "size": audio.stat().st_size,
            }
        )
        samples.append(
            {
                "audio": f"clips/{index:02d}.wav",
                "text": f"Synthetic exact transcript {index}.",
                "duration": 30.0,
            }
        )
        derivations.append(
            {
                "output_path": f"clips/{index:02d}.wav",
                "output_sha256": _digest(audio),
                "output_size_bytes": audio.stat().st_size,
                "split": "train",
            }
        )
    train_manifest = tmp_path / "training" / "train.jsonl"
    train_manifest.write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples),
        encoding="utf-8",
    )
    dataset_manifest = tmp_path / "training" / "dataset-manifest.json"
    _json(
        dataset_manifest,
        {
            "schema_version": 1,
            "kind": "echoweave-voxcpm2-training-dataset",
            "derivations": derivations,
        },
    )
    result = gpu_worker._validate_training_manifest(
        {"path": train_manifest}, {"path": dataset_manifest}, artifacts
    )
    assert result == {"sample_count": 10, "duration_seconds": 300.0}

    samples[1]["audio"] = samples[0]["audio"]
    train_manifest.write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples),
        encoding="utf-8",
    )
    with pytest.raises(gpu_worker.WorkerPackError, match="repeats"):
        gpu_worker._validate_training_manifest(
            {"path": train_manifest}, {"path": dataset_manifest}, artifacts
        )


def test_lora_manifest_rejects_dataset_escape_and_derivation_mismatch(tmp_path: Path):
    training = tmp_path / "training"
    clips = training / "clips"
    artifacts = []
    samples = []
    derivations = []
    for index in range(10):
        audio = clips / f"{index:02d}.wav"
        _wav(audio, 30.0)
        digest = _digest(audio)
        artifacts.append(
            {
                "path": audio,
                "role": "voxcpm_train_audio",
                "sha256": digest,
                "size": audio.stat().st_size,
            }
        )
        samples.append(
            {
                "audio": f"clips/{index:02d}.wav",
                "text": f"Synthetic exact transcript {index}.",
                "duration": 30.0,
            }
        )
        derivations.append(
            {
                "output_path": f"clips/{index:02d}.wav",
                "output_sha256": digest,
                "output_size_bytes": audio.stat().st_size,
                "split": "train",
            }
        )
    train_manifest = training / "train.jsonl"
    dataset_manifest = training / "dataset-manifest.json"

    escaped = tmp_path / "escaped.wav"
    _wav(escaped, 30.0)
    samples[0]["audio"] = "../escaped.wav"
    train_manifest.write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples),
        encoding="utf-8",
    )
    _json(
        dataset_manifest,
        {
            "schema_version": 1,
            "kind": "echoweave-voxcpm2-training-dataset",
            "derivations": derivations,
        },
    )
    with pytest.raises(gpu_worker.WorkerPackError, match="escapes"):
        gpu_worker._validate_training_manifest(
            {"path": train_manifest}, {"path": dataset_manifest}, artifacts
        )

    samples[0]["audio"] = "clips/00.wav"
    derivations[0]["output_sha256"] = "0" * 64
    train_manifest.write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples),
        encoding="utf-8",
    )
    _json(
        dataset_manifest,
        {
            "schema_version": 1,
            "kind": "echoweave-voxcpm2-training-dataset",
            "derivations": derivations,
        },
    )
    with pytest.raises(gpu_worker.WorkerPackError, match="dataset derivation"):
        gpu_worker._validate_training_manifest(
            {"path": train_manifest}, {"path": dataset_manifest}, artifacts
        )


def test_lora_manifest_rejects_linked_audio(tmp_path: Path):
    training = tmp_path / "training"
    clips = training / "clips"
    target = clips / "target.wav"
    _wav(target, 30.0)
    link = clips / "linked.wav"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable on this host")
    train_manifest = training / "train.jsonl"
    train_manifest.write_text(
        json.dumps({"audio": "clips/linked.wav", "text": "Synthetic transcript."})
        + "\n",
        encoding="utf-8",
    )
    dataset_manifest = training / "dataset-manifest.json"
    _json(
        dataset_manifest,
        {
            "schema_version": 1,
            "kind": "echoweave-voxcpm2-training-dataset",
            "derivations": [
                {
                    "output_path": "clips/linked.wav",
                    "output_sha256": _digest(target),
                    "output_size_bytes": target.stat().st_size,
                    "split": "train",
                }
            ],
        },
    )
    with pytest.raises(
        gpu_worker.WorkerPackError, match="symbolic link|traverse a link"
    ):
        gpu_worker._validate_training_manifest(
            {"path": train_manifest},
            {"path": dataset_manifest},
            [
                {
                    "path": link,
                    "role": "voxcpm_train_audio",
                    "sha256": _digest(target),
                    "size": target.stat().st_size,
                }
            ],
        )


def test_lora_config_rejects_zero_step_and_unbound_paths(tmp_path: Path):
    model = tmp_path / "model"
    manifest = tmp_path / "training" / "train.jsonl"
    checkpoint = tmp_path / "checkpoints"
    config = tmp_path / "lora.yaml"
    template = (
        gpu_worker.WORKSPACE_ROOT / "gpu_worker_pack" / "configs" / "voxcpm2-lora.yaml"
    ).read_text(encoding="utf-8")
    locked = (
        template.replace("/models/VoxCPM2", str(model))
        .replace("/private/echoweave/job/train.jsonl", str(manifest))
        .replace("/private/echoweave/job/checkpoints/voxcpm2-lora", str(checkpoint))
    )
    config.write_text(locked, encoding="utf-8")
    gpu_worker._validate_lora_config(
        config,
        model_path=model,
        train_manifest=manifest,
        checkpoint_dir=checkpoint,
    )

    config.write_text(
        locked.replace("num_iters: 1000", "num_iters: 0"), encoding="utf-8"
    )
    with pytest.raises(gpu_worker.WorkerPackError, match="values are not locked"):
        gpu_worker._validate_lora_config(
            config,
            model_path=model,
            train_manifest=manifest,
            checkpoint_dir=checkpoint,
        )

    config.write_text(
        locked.replace("val_manifest: null", "val_manifest: /private/unbound.jsonl"),
        encoding="utf-8",
    )
    with pytest.raises(gpu_worker.WorkerPackError, match="values are not locked"):
        gpu_worker._validate_lora_config(
            config,
            model_path=model,
            train_manifest=manifest,
            checkpoint_dir=checkpoint,
        )


def test_preflight_enforces_24_gib_for_lora(monkeypatch, tmp_path: Path):
    model_root = tmp_path / "model"
    model_root.mkdir()
    for name in ("config.json", "model.safetensors", "audiovae.pth"):
        (model_root / name).write_bytes(b"fixture")
    validated = {
        "run_id": "lora-hardware-check",
        "mode": gpu_worker.MODE_VOX_LORA,
        "execution": {
            "gpu_index": 0,
            "output_dir": tmp_path / "outputs",
            "output_filename": "candidate.wav",
            "checkpoint_dir": tmp_path / "checkpoints",
        },
        "models": [{"id": gpu_worker.VOXCPM_MODEL_ID, "path": model_root}],
        "repositories": [],
    }
    monkeypatch.setattr(
        gpu_worker,
        "_gpu_inventory",
        lambda: [
            {
                "index": 0,
                "name": "four-gib-test-gpu",
                "memory_total_mib": 4096,
                "memory_free_mib": 4096,
                "driver_version": "test",
                "compute_capability": "6.1",
            }
        ],
    )
    monkeypatch.setattr(gpu_worker, "_package_version", lambda _name: None)

    report = gpu_worker.preflight_job(validated)
    by_name = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is False
    assert by_name["gpu_memory_total"]["ok"] is False
    assert "24 GiB" in by_name["gpu_memory_total"]["detail"]
    assert by_name["gpu_native_bfloat16"]["ok"] is False
    assert by_name["linux"]["ok"] is gpu_worker.sys.platform.startswith("linux")


@pytest.mark.parametrize(
    ("compute_capability", "expected"),
    [("6.1", False), ("7.5", False), ("8.0", True), ("8.9", True), ("bad", False)],
)
def test_native_bfloat16_requires_ampere_or_newer(compute_capability, expected):
    gpu = {"compute_capability": compute_capability}

    assert gpu_worker._supports_native_bfloat16(gpu) is expected


def test_gpu_inventory_captures_compute_capability(monkeypatch):
    result = gpu_worker.subprocess.CompletedProcess(
        ["nvidia-smi"],
        0,
        "0, NVIDIA RTX TEST, 24576, 24000, 999.1, 8.9\n",
        "",
    )
    monkeypatch.setattr(gpu_worker, "_run_capture", lambda _command: result)

    assert gpu_worker._gpu_inventory() == [
        {
            "index": 0,
            "name": "NVIDIA RTX TEST",
            "memory_total_mib": 24576,
            "memory_free_mib": 24000,
            "driver_version": "999.1",
            "compute_capability": "8.9",
        }
    ]


def test_preflight_binds_voxcpm_scm_version_to_audited_checkout(
    monkeypatch, tmp_path: Path
):
    model_root = tmp_path / "model"
    model_root.mkdir()
    for name in ("config.json", "model.safetensors", "audiovae.pth"):
        (model_root / name).write_bytes(b"fixture")
    repository = tmp_path / "VoxCPM"
    module = repository / "src" / "voxcpm" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    entrypoint = repository / "scripts" / "train_voxcpm_finetune.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("", encoding="utf-8")
    validated = {
        "run_id": "lora-source-check",
        "mode": gpu_worker.MODE_VOX_LORA,
        "execution": {
            "gpu_index": 0,
            "output_dir": tmp_path / "outputs",
            "output_filename": "candidate.wav",
            "checkpoint_dir": tmp_path / "checkpoints",
        },
        "models": [{"id": gpu_worker.VOXCPM_MODEL_ID, "path": model_root}],
        "repositories": [
            {
                "id": gpu_worker.VOXCPM_REPO_ID,
                "revision": gpu_worker.VOXCPM_REPO_REVISION,
                "path": repository,
            }
        ],
    }
    versions = {
        "torch": "2.7.1",
        "voxcpm": gpu_worker.VOXCPM_PACKAGE_VERSION,
        "soundfile": "0.13.1",
    }
    monkeypatch.setattr(
        gpu_worker,
        "_gpu_inventory",
        lambda: [
            {
                "index": 0,
                "name": "qualification-gpu",
                "memory_total_mib": 24 * 1024,
                "memory_free_mib": 22 * 1024,
                "driver_version": "test",
                "compute_capability": "8.9",
            }
        ],
    )
    monkeypatch.setattr(gpu_worker, "_package_version", lambda name: versions.get(name))
    monkeypatch.setattr(gpu_worker, "_module_origin", lambda _name: module.resolve())

    def fake_run(command, *, cwd=None):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return gpu_worker.subprocess.CompletedProcess(
                command, 0, gpu_worker.VOXCPM_REPO_REVISION + "\n", ""
            )
        if command[:3] == ["git", "status", "--porcelain"]:
            return gpu_worker.subprocess.CompletedProcess(command, 0, "", "")
        return gpu_worker.subprocess.CompletedProcess(command, 127, "", "unsupported")

    monkeypatch.setattr(gpu_worker, "_run_capture", fake_run)

    report = gpu_worker.preflight_job(validated)
    by_name = {item["name"]: item for item in report["checks"]}

    assert by_name["voxcpm"]["ok"] is True
    assert by_name["voxcpm_source_checkout"]["ok"] is True

    versions["voxcpm"] = "2.0.3"
    outside = tmp_path / "site-packages" / "voxcpm" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    monkeypatch.setattr(gpu_worker, "_module_origin", lambda _name: outside.resolve())

    rejected = gpu_worker.preflight_job(validated)
    rejected_by_name = {item["name"]: item for item in rejected["checks"]}

    assert rejected_by_name["voxcpm"]["ok"] is False
    assert rejected_by_name["voxcpm_source_checkout"]["ok"] is False


def test_soulx_manifest_requires_lite_pins_and_normalized_inputs(tmp_path: Path):
    authorization, authorization_artifact = _authorization(
        tmp_path,
        scopes=["avatar_animation"],
        processors=[gpu_worker.SOULX_MODEL_ID, gpu_worker.WAV2VEC_MODEL_ID],
    )
    image = tmp_path / "inputs" / "avatar.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + (512).to_bytes(4, "big")
        + (512).to_bytes(4, "big")
    )
    driving = tmp_path / "inputs" / "driving.wav"
    _wav(driving, 1.0)
    soulx_model = _model(
        tmp_path,
        model_id=gpu_worker.SOULX_MODEL_ID,
        revision=gpu_worker.SOULX_MODEL_REVISION,
        files=(
            "Model_Lite/config.json",
            "Model_Lite/diffusion_pytorch_model.safetensors",
            "VAE_LTX/config.json",
            "VAE_LTX/diffusion_pytorch_model.safetensors",
        ),
    )
    wav2vec_model = _model(
        tmp_path,
        model_id=gpu_worker.WAV2VEC_MODEL_ID,
        revision=gpu_worker.WAV2VEC_MODEL_REVISION,
        files=("config.json", "model.safetensors"),
    )
    repository = tmp_path / "source" / "SoulX-FlashHead"
    repository.mkdir(parents=True)
    job = {
        "schema_version": 1,
        "run_id": "synthetic-avatar-v1",
        "mode": gpu_worker.MODE_SOULX_LITE,
        "authorization": authorization,
        "models": [soulx_model, wav2vec_model],
        "source_repositories": [
            {
                "id": gpu_worker.SOULX_REPO_ID,
                "revision": gpu_worker.SOULX_REPO_REVISION,
                "path": str(repository),
            }
        ],
        "inputs": [
            authorization_artifact,
            _artifact(image, "avatar", "avatar_reference_png", "image/png"),
            _artifact(driving, "driving", "driving_audio_wav", "audio/wav"),
        ],
        "execution": {
            "gpu_index": 0,
            "seed": 42,
            "output_dir": str(tmp_path / "outputs"),
            "output_filename": "avatar.mp4",
        },
    }
    manifest = tmp_path / "soulx-job.json"
    _json(manifest, job)

    validated = gpu_worker.validate_job(manifest, now=NOW)

    assert validated["mode_details"]["avatar_width"] == 512
    assert validated["mode_details"]["driving_audio_duration_seconds"] == 1.0


@pytest.mark.parametrize(
    ("mode", "metrics"),
    [
        (
            gpu_worker.MODE_VOX_ZERO,
            {
                "speaker_embedding_cosine": 0.85,
                "asr_character_error_rate": 0.05,
                "rtf_p95": 0.5,
                "clipping_ratio": 0.0,
                "sample_rate_hz": 48_000,
                "human_review_pass": True,
                "ai_disclosure_present": True,
            },
        ),
        (
            gpu_worker.MODE_SOULX_LITE,
            {
                "face_identity_cosine": 0.8,
                "syncnet_lse_c": 6.0,
                "fps": 25.0,
                "rtf_p95": 0.9,
                "dropped_frame_ratio": 0.0,
                "watermark_present": True,
                "human_review_pass": True,
                "ai_disclosure_present": True,
            },
        ),
    ],
)
def test_acceptance_profiles_are_fixed(tmp_path: Path, mode: str, metrics: dict):
    payload = {
        "schema_version": 1,
        "evaluators": [_evaluator(tmp_path)],
        "metrics": metrics,
    }
    assert gpu_worker.evaluate_metrics(mode, payload)["passed"] is True

    first_numeric = next(
        key for key, value in metrics.items() if isinstance(value, (int, float))
    )
    metrics[first_numeric] = -100.0
    assert gpu_worker.evaluate_metrics(mode, payload)["passed"] is False


def test_lora_must_beat_zero_shot_baseline(tmp_path: Path):
    payload = {
        "schema_version": 1,
        "evaluators": [_evaluator(tmp_path)],
        "metrics": {
            "speaker_embedding_cosine": 0.86,
            "asr_character_error_rate": 0.05,
            "rtf_p95": 0.6,
            "clipping_ratio": 0.0,
            "sample_rate_hz": 48_000,
            "speaker_cosine_delta_vs_zero_shot": 0.01,
            "cer_delta_vs_zero_shot": 0.0,
            "human_review_pass": True,
            "ai_disclosure_present": True,
        },
    }

    result = gpu_worker.evaluate_metrics(gpu_worker.MODE_VOX_LORA, payload)

    assert result["passed"] is False
    delta = next(
        check
        for check in result["checks"]
        if check["metric"] == "speaker_cosine_delta_vs_zero_shot"
    )
    assert delta["passed"] is False


def test_metrics_reject_nonfinite_values_and_changed_evaluator(tmp_path: Path):
    evaluator = _evaluator(tmp_path)
    payload = {
        "schema_version": 1,
        "evaluators": [evaluator],
        "metrics": {
            "speaker_embedding_cosine": float("inf"),
            "asr_character_error_rate": 0.05,
            "rtf_p95": 0.5,
            "clipping_ratio": 0.0,
            "sample_rate_hz": 48_000,
            "human_review_pass": True,
            "ai_disclosure_present": True,
        },
    }
    result = gpu_worker.evaluate_metrics(gpu_worker.MODE_VOX_ZERO, payload)
    assert result["passed"] is False

    Path(evaluator["path"]).write_bytes(b"changed")
    with pytest.raises(gpu_worker.WorkerPackError, match="artifact hash"):
        gpu_worker.evaluate_metrics(gpu_worker.MODE_VOX_ZERO, payload)


def test_provenance_omits_private_paths_and_text(monkeypatch, tmp_path: Path):
    manifest, _ = _zero_job(tmp_path)
    validated = gpu_worker.validate_job(manifest, now=NOW)
    media = tmp_path / "outputs" / "candidate.wav"
    _wav(media, 1.0)
    run_record = tmp_path / "private-run-record.json"
    binding = {
        "phase": "job execution",
        "verified_at": "2026-08-03T08:00:00Z",
        "manifest_sha256": validated["sha256"],
        "artifacts": [
            {
                "id": artifact["id"],
                "role": artifact["role"],
                "sha256": artifact["sha256"],
                "size": artifact["size"],
            }
            for artifact in validated["artifacts"]
        ],
        "models": [
            {
                "id": model["id"],
                "revision": model["revision"],
                "snapshot_sha256": model["snapshot_sha256"],
                "file_count": model["file_count"],
            }
            for model in validated["models"]
        ],
        "source_repositories": [
            {"id": repo["id"], "revision": repo["revision"]}
            for repo in validated["repositories"]
        ],
    }
    monkeypatch.setattr(
        gpu_worker,
        "_verify_validated_binding",
        lambda _validated, *, phase: {**binding, "phase": phase},
    )
    key = b"test-run-attestation-key-with-32-bytes-minimum"
    monkeypatch.setenv(gpu_worker.RUN_ATTESTATION_KEY_ENV, key.decode())
    record_payload = gpu_worker._attach_run_attestation(
        {
            "schema_version": 1,
            "run_id": validated["run_id"],
            "mode": validated["mode"],
            "manifest_sha256": validated["sha256"],
            "started_at": "2026-08-03T08:00:00Z",
            "completed_at": "2026-08-03T08:00:01Z",
            "status": "completed",
            "preflight": {
                "gpu": {"name": "test", "memory_total_mib": 24576},
                "versions": {"echoweave": "test"},
                "platform": {"system": "Linux", "machine": "x86_64"},
            },
            "input_binding": binding,
            "outputs": [
                {
                    "kind": "qualified_media_candidate",
                    "path": str(media),
                    "sha256": _digest(media),
                    "size": media.stat().st_size,
                }
            ],
        },
        key,
    )
    _json(run_record, record_payload)
    metrics_path = tmp_path / "metrics.json"
    _json(
        metrics_path,
        {
            "schema_version": 1,
            "run_id": validated["run_id"],
            "evaluators": [_evaluator(tmp_path)],
            "metrics": {
                "speaker_embedding_cosine": 0.85,
                "asr_character_error_rate": 0.05,
                "rtf_p95": 0.5,
                "clipping_ratio": 0.0,
                "sample_rate_hz": 48_000,
                "human_review_pass": True,
                "ai_disclosure_present": True,
            },
        },
    )
    provenance_path = tmp_path / "provenance.json"

    provenance = gpu_worker.finalize_provenance(
        validated,
        record_path=run_record,
        metrics_path=metrics_path,
        output_path=provenance_path,
    )

    serialized = json.dumps(provenance)
    assert provenance["accepted"] is True
    assert str(tmp_path) not in serialized
    assert "Synthetic qualification sentence" not in serialized
    assert provenance["outputs"][0]["name"] == "candidate.wav"

    tampered_record = tmp_path / "tampered-run-record.json"
    record_payload["attestation"]["signature"] = "0" * 64
    _json(tampered_record, record_payload)
    with pytest.raises(gpu_worker.WorkerPackError, match="attestation"):
        gpu_worker.finalize_provenance(
            validated,
            record_path=tampered_record,
            metrics_path=metrics_path,
            output_path=tmp_path / "tampered-provenance.json",
        )
