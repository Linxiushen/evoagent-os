from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transcribe_qwen_ranges.py"
SPEC = importlib.util.spec_from_file_location("transcribe_qwen_ranges", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
MODEL_REVISION = "7" * 40


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_wav(
    path: Path,
    *,
    seconds: float = 10.0,
    channels: int = 1,
    sample_width: int = 2,
    sample_rate: int = MODULE.SAMPLE_RATE,
) -> None:
    frame_count = round(seconds * sample_rate)
    frame = b"\x10\x00" * channels if sample_width == 2 else b"\x10" * channels
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.setframerate(sample_rate)
        stream.writeframes(frame * frame_count)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    audio = tmp_path / "source.wav"
    ranges = tmp_path / "ranges.json"
    model = tmp_path / "model"
    output = tmp_path / "transcript.json"
    _write_wav(audio)
    model.mkdir()
    _write_json(
        ranges,
        {
            "schema_version": 1,
            "source_sha256": _digest(audio),
            "segments": [
                {
                    "clip_id": "clip-001",
                    "start_seconds": 0.0,
                    "end_seconds": 3.0,
                },
                {
                    "clip_id": "clip-002",
                    "start_seconds": 3.0,
                    "end_seconds": 6.0,
                },
                {
                    "clip_id": "clip-003",
                    "start_seconds": 6.0,
                    "end_seconds": 9.0,
                },
            ],
        },
    )
    return {"audio": audio, "ranges": ranges, "model": model, "output": output}


class FakeModel:
    def __init__(self, *, fail_at: int | None = None, mutate=None) -> None:
        self.fail_at = fail_at
        self.mutate = mutate
        self.calls = 0
        self.frame_counts: list[int] = []

    def transcribe(self, *, audio, language):
        assert language is None
        samples, sample_rate = audio
        assert sample_rate == MODULE.SAMPLE_RATE
        self.calls += 1
        self.frame_counts.append(len(samples))
        if self.mutate is not None:
            self.mutate(self.calls)
        if self.fail_at == self.calls:
            raise RuntimeError("synthetic inference failure")
        return [
            SimpleNamespace(
                language="Chinese",
                text=f"machine transcript {self.calls}",
            )
        ]


def _run(paths: dict[str, Path], monkeypatch, *, model=None, **overrides) -> Path:
    fake_model = model or FakeModel()
    monkeypatch.setattr(MODULE, "_load_model", lambda *_args: fake_model)
    arguments = {
        "audio_path": paths["audio"],
        "ranges_path": paths["ranges"],
        "model_path": paths["model"],
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "output_path": paths["output"],
    }
    arguments.update(overrides)
    return MODULE.transcribe_ranges(**arguments)


def _assert_no_review_fields(value: object) -> None:
    forbidden = {
        "human_review",
        "reviewed",
        "approved",
        "approved_for_training",
        "transcript_verified",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for child in value.values():
            _assert_no_review_fields(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_review_fields(child)


def test_transcribes_each_range_and_writes_provenance(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    model = FakeModel()

    result = _run(paths, monkeypatch, model=model)

    assert result == paths["output"].resolve()
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["kind"] == MODULE.KIND
    assert payload["source_path"] == str(paths["audio"].resolve())
    assert payload["source_sha256"] == _digest(paths["audio"])
    assert payload["ranges_path"] == str(paths["ranges"].resolve())
    assert payload["ranges_sha256"] == _digest(paths["ranges"])
    assert payload["model"] == MODEL_ID
    assert payload["model_revision"] == MODEL_REVISION
    assert payload["model_path"] == str(paths["model"].resolve())
    assert payload["device"] == "cpu"
    assert [item["clip_id"] for item in payload["segments"]] == [
        "clip-001",
        "clip-002",
        "clip-003",
    ]
    assert all(item["language"] == "Chinese" for item in payload["segments"])
    assert all(item["text"] for item in payload["segments"])
    assert model.frame_counts == [48_000, 48_000, 48_000]
    assert not list(tmp_path.glob(".transcript.json.*.tmp"))
    _assert_no_review_fields(payload)


def test_failure_checkpoints_exact_prefix_and_resume_only_processes_suffix(
    tmp_path, monkeypatch
):
    paths = _fixture(tmp_path)
    first_model = FakeModel(fail_at=2)

    with pytest.raises(
        MODULE.QwenRangeTranscriptionError, match="transcription failed"
    ):
        _run(paths, monkeypatch, model=first_model)

    prefix = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert [item["clip_id"] for item in prefix["segments"]] == ["clip-001"]

    resumed_model = FakeModel()
    _run(paths, monkeypatch, model=resumed_model)

    completed = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert [item["clip_id"] for item in completed["segments"]] == [
        "clip-001",
        "clip-002",
        "clip-003",
    ]
    assert resumed_model.calls == 2


def test_completed_output_returns_without_loading_qwen(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    _run(paths, monkeypatch)
    before = paths["output"].read_bytes()

    def unexpected_load(*_args):
        raise AssertionError("completed resume must not load Qwen")

    monkeypatch.setattr(MODULE, "_load_model", unexpected_load)
    result = MODULE.transcribe_ranges(
        audio_path=paths["audio"],
        ranges_path=paths["ranges"],
        model_path=paths["model"],
        model_id=MODEL_ID,
        model_revision=MODEL_REVISION,
        output_path=paths["output"],
    )

    assert result == paths["output"].resolve()
    assert paths["output"].read_bytes() == before


def test_model_loader_is_pinned_to_local_files(tmp_path, monkeypatch):
    calls = []
    expected_model = object()

    class FakeQwenModel:
        @classmethod
        def from_pretrained(cls, model_path, **kwargs):
            calls.append((model_path, kwargs))
            return expected_model

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(float32="fp32", bfloat16="bf16"),
    )
    monkeypatch.setitem(
        sys.modules,
        "qwen_asr",
        SimpleNamespace(Qwen3ASRModel=FakeQwenModel),
    )
    model_path = tmp_path / "model"
    model_path.mkdir()

    loaded = MODULE._load_model(model_path, MODEL_REVISION, "cpu")

    assert loaded is expected_model
    assert calls == [
        (
            str(model_path),
            {
                "revision": MODEL_REVISION,
                "local_files_only": True,
                "dtype": "fp32",
                "device_map": "cpu",
                "max_inference_batch_size": 1,
                "max_new_tokens": 256,
            },
        )
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(model="different/model"),
        lambda payload: payload["segments"].reverse(),
        lambda payload: payload["segments"].pop(0),
        lambda payload: payload["segments"][0].update(end_seconds=4.0),
        lambda payload: payload["segments"].append(payload["segments"][0].copy()),
        lambda payload: payload.update(human_review=True),
        lambda payload: payload["segments"][0].update(approved=True),
    ],
    ids=[
        "model-binding",
        "reordered",
        "skipped-prefix",
        "altered-range",
        "extra-segment",
        "review-field",
        "nested-approval-field",
    ],
)
def test_incompatible_resume_is_rejected_without_modifying_output(
    tmp_path, monkeypatch, mutation
):
    paths = _fixture(tmp_path)
    _run(paths, monkeypatch)
    payload = json.loads(paths["output"].read_text(encoding="utf-8"))
    mutation(payload)
    _write_json(paths["output"], payload)
    before = paths["output"].read_bytes()

    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="existing output"):
        _run(paths, monkeypatch)

    assert paths["output"].read_bytes() == before


@pytest.mark.parametrize(
    "segments, message",
    [
        (
            [{"clip_id": "clip", "start_seconds": 0.0, "end_seconds": 2.99}],
            "duration",
        ),
        (
            [{"clip_id": "clip", "start_seconds": 8.0, "end_seconds": 11.0}],
            "exceeds",
        ),
        (
            [
                {"clip_id": "later", "start_seconds": 4.0, "end_seconds": 7.0},
                {"clip_id": "earlier", "start_seconds": 0.0, "end_seconds": 3.0},
            ],
            "sorted",
        ),
        (
            [
                {"clip_id": "one", "start_seconds": 0.0, "end_seconds": 4.0},
                {"clip_id": "two", "start_seconds": 3.0, "end_seconds": 6.0},
            ],
            "overlapping",
        ),
        (
            [
                {"clip_id": "same", "start_seconds": 0.0, "end_seconds": 3.0},
                {"clip_id": "same", "start_seconds": 3.0, "end_seconds": 6.0},
            ],
            "duplicate clip_id",
        ),
        (
            [
                {"clip_id": "Clip", "start_seconds": 0.0, "end_seconds": 3.0},
                {"clip_id": "clip", "start_seconds": 3.0, "end_seconds": 6.0},
            ],
            "filename collision",
        ),
        (
            [{"clip_id": "clip", "start_seconds": True, "end_seconds": 3.0}],
            "finite JSON number",
        ),
    ],
)
def test_rejects_invalid_ranges_before_loading_model(
    tmp_path, monkeypatch, segments, message
):
    paths = _fixture(tmp_path)
    _write_json(
        paths["ranges"],
        {
            "schema_version": 1,
            "source_sha256": _digest(paths["audio"]),
            "segments": segments,
        },
    )
    monkeypatch.setattr(
        MODULE,
        "_load_model",
        lambda *_args: pytest.fail("invalid ranges must not load Qwen"),
    )

    with pytest.raises(MODULE.QwenRangeTranscriptionError, match=message):
        MODULE.transcribe_ranges(
            audio_path=paths["audio"],
            ranges_path=paths["ranges"],
            model_path=paths["model"],
            model_id=MODEL_ID,
            model_revision=MODEL_REVISION,
            output_path=paths["output"],
        )

    assert not paths["output"].exists()


def test_rejects_extra_range_fields_and_source_hash_mismatch(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    payload = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    payload["source_path"] = "unsupported.wav"
    _write_json(paths["ranges"], payload)

    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="unsupported fields"):
        _run(paths, monkeypatch)

    payload.pop("source_path")
    payload["source_sha256"] = "0" * 64
    _write_json(paths["ranges"], payload)
    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="does not match"):
        _run(paths, monkeypatch)


@pytest.mark.parametrize(
    "audio_kwargs",
    [
        {"channels": 2},
        {"sample_width": 1},
        {"sample_rate": 8_000},
    ],
)
def test_rejects_nonconforming_wav(tmp_path, monkeypatch, audio_kwargs):
    paths = _fixture(tmp_path)
    _write_wav(paths["audio"], **audio_kwargs)
    payload = json.loads(paths["ranges"].read_text(encoding="utf-8"))
    payload["source_sha256"] = _digest(paths["audio"])
    _write_json(paths["ranges"], payload)

    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="mono PCM16 16kHz"):
        _run(paths, monkeypatch)


def test_source_mutation_preserves_last_verified_checkpoint(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)

    def mutate_on_second_call(call: int) -> None:
        if call == 2:
            with paths["audio"].open("r+b") as stream:
                stream.seek(-2, os.SEEK_END)
                stream.write(b"\x20\x00")

    model = FakeModel(mutate=mutate_on_second_call)
    with pytest.raises(
        MODULE.QwenRangeTranscriptionError, match="source audio changed"
    ):
        _run(paths, monkeypatch, model=model)

    payload = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert [item["clip_id"] for item in payload["segments"]] == ["clip-001"]


def test_rejects_symlinked_input_when_supported(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    linked_audio = tmp_path / "linked.wav"
    try:
        linked_audio.symlink_to(paths["audio"])
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is not available")

    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="symbolic links"):
        _run(paths, monkeypatch, audio_path=linked_audio)


def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(
    tmp_path, monkeypatch
):
    paths = _fixture(tmp_path)
    digest = _digest(paths["audio"])
    paths["ranges"].write_text(
        '{"schema_version":1,"schema_version":1,'
        f'"source_sha256":"{digest}","segments":[]}}',
        encoding="utf-8",
    )
    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="duplicate JSON key"):
        _run(paths, monkeypatch)

    paths["ranges"].write_text(
        '{"schema_version":1,'
        f'"source_sha256":"{digest}",'
        '"segments":[{"clip_id":"clip","start_seconds":NaN,'
        '"end_seconds":3.0}]}',
        encoding="utf-8",
    )
    with pytest.raises(MODULE.QwenRangeTranscriptionError, match="numeric constant"):
        _run(paths, monkeypatch)
