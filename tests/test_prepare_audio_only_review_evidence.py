from __future__ import annotations

import hashlib
import importlib.util
import json
import wave
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "prepare_audio_only_review_evidence.py"
)
SPEC = importlib.util.spec_from_file_location(
    "prepare_audio_only_review_evidence", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wav(path: Path, seconds: int = 10) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(MODULE.SAMPLE_RATE)
        stream.writeframes(b"\x10\x00" * MODULE.SAMPLE_RATE * seconds)


def _write_transcript(path: Path, audio: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": MODULE.TRANSCRIPT_KIND,
                "source_path": str(audio.resolve()),
                "source_sha256": _digest(audio),
                "segments": [
                    {
                        "clip_id": "clip-0001",
                        "start_seconds": 1.0,
                        "end_seconds": 5.0,
                        "duration_seconds": 4.0,
                        "language": "Chinese",
                        "text": "Machine transcript.",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_prepares_explicit_audio_only_evidence(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    transcript = tmp_path / "transcript.json"
    output = tmp_path / "evidence.json"
    _write_wav(audio)
    _write_transcript(transcript, audio)

    MODULE.prepare_audio_only_evidence(
        audio_path=audio, transcript_path=transcript, output_path=output
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["kind"] == MODULE.KIND
    assert payload["input"]["sha256"] == _digest(audio)
    assert payload["input"]["video_stream"] is False
    assert payload["segments"]["sha256"] == _digest(transcript)
    assert payload["method"] == {
        "engine": "none",
        "ocr_performed": False,
        "reason_code": "audio_only_no_video_stream",
    }
    assert payload["alignment"] == [
        {
            "clip_id": "clip-0001",
            "start_seconds": 1.0,
            "end_seconds": 5.0,
            "asr_text": "Machine transcript.",
            "subtitle_text": "",
            "similarity": 0.0,
            "caption_count": 0,
        }
    ]


def test_rejects_transcript_bound_to_other_audio(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    other = tmp_path / "other.wav"
    transcript = tmp_path / "transcript.json"
    _write_wav(audio)
    _write_wav(other, seconds=11)
    _write_transcript(transcript, other)

    with pytest.raises(MODULE.AudioOnlyEvidenceError, match="source hash"):
        MODULE.prepare_audio_only_evidence(
            audio_path=audio,
            transcript_path=transcript,
            output_path=tmp_path / "evidence.json",
        )


def test_refuses_to_overwrite_output(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    transcript = tmp_path / "transcript.json"
    output = tmp_path / "evidence.json"
    _write_wav(audio)
    _write_transcript(transcript, audio)
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(MODULE.AudioOnlyEvidenceError, match="already exists"):
        MODULE.prepare_audio_only_evidence(
            audio_path=audio, transcript_path=transcript, output_path=output
        )
    assert output.read_text(encoding="utf-8") == "keep"


def test_rejects_non_pcm16_audio(tmp_path: Path) -> None:
    audio = tmp_path / "source.wav"
    transcript = tmp_path / "transcript.json"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(MODULE.SAMPLE_RATE)
        stream.writeframes(b"\x00\x00\x00\x00" * MODULE.SAMPLE_RATE * 10)
    _write_transcript(transcript, audio)

    with pytest.raises(MODULE.AudioOnlyEvidenceError, match="mono PCM16"):
        MODULE.prepare_audio_only_evidence(
            audio_path=audio,
            transcript_path=transcript,
            output_path=tmp_path / "evidence.json",
        )
