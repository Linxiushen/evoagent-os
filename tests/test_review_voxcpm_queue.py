from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import sys
import threading
import wave
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_voxcpm_queue.py"
SPEC = importlib.util.spec_from_file_location("review_voxcpm_queue", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wav(path: Path, seconds: float = 3.0) -> None:
    frames = round(16_000 * seconds)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x20\x00" * frames)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _queue(tmp_path: Path, *, clip_count: int = 2, audio_only: bool = False) -> Path:
    root = tmp_path / "immutable-queue"
    clips_dir = root / "clips"
    clips_dir.mkdir(parents=True)
    clips = []
    for index in range(1, clip_count + 1):
        clip_id = f"clip-{index:03d}"
        audio = clips_dir / f"{clip_id}.wav"
        _write_wav(audio)
        clips.append(
            {
                "clip_id": clip_id,
                "audio_path": f"clips/{clip_id}.wav",
                "audio_sha256": _digest(audio),
                "audio_size_bytes": audio.stat().st_size,
                "start_seconds": float((index - 1) * 3),
                "end_seconds": float(index * 3),
                "duration_seconds": 3.0,
                "candidate_transcript": f"Machine transcript {index}.",
                "ocr_evidence": {
                    "subtitle_text": ""
                    if audio_only
                    else f"Subtitle evidence {index}.",
                    "similarity": 0.0 if audio_only else 0.75,
                    "caption_count": 0 if audio_only else 2,
                },
                "human_review": False,
                "approved": False,
                "approved_for_training": False,
                "transcript_verified": False,
            }
        )
    manifest = {
        "schema_version": 1,
        "kind": MODULE.QUEUE_KIND,
        "review_only": True,
        "training_ready": False,
        "ready_for_local_training": False,
        "runtime_promotion_allowed": False,
        "human_review": False,
        "approved": False,
        "approved_for_training": False,
        "transcript_verified": False,
        "review_evidence": {
            "kind": MODULE.AUDIO_ONLY_KIND
            if audio_only
            else MODULE.BURNED_SUBTITLE_KIND,
            "ocr_performed": not audio_only,
            "reason_code": "audio_only_no_video_stream" if audio_only else None,
        },
        "clips": clips,
    }
    manifest_path = root / "review-queue.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def _decision(clip_id: str, *, complete: bool = False) -> dict:
    return {
        "clip_id": clip_id,
        "corrected_text": "Human corrected transcript." if complete else "",
        "target_speaker_only": complete,
        "transcript_verified": complete,
        "no_third_party_speech": complete,
        "no_background_music": complete,
        "approved": complete,
    }


def test_loads_immutable_queue_and_defaults_every_decision_false(tmp_path):
    manifest_path = _queue(tmp_path)

    queue = MODULE.QueueSnapshot.load(manifest_path)
    payload = queue.public_payload({})

    assert queue.root == manifest_path.parent.resolve()
    assert payload["total_clips"] == 2
    assert payload["review_gates_satisfied"] == 0
    assert payload["all_review_gates_satisfied"] is False
    assert payload["review_evidence"]["ocr_performed"] is True
    for clip in payload["clips"]:
        decision = clip["decision"]
        assert decision["corrected_text"] == ""
        assert decision["target_speaker_only"] is False
        assert decision["transcript_verified"] is False
        assert decision["no_third_party_speech"] is False
        assert decision["no_background_music"] is False
        assert decision["approved"] is False
        assert decision["gates_satisfied"] is False


def test_public_payload_preserves_audio_only_evidence_status(tmp_path):
    queue = MODULE.QueueSnapshot.load(_queue(tmp_path, audio_only=True))

    payload = queue.public_payload({})

    assert payload["review_evidence"] == {
        "kind": MODULE.AUDIO_ONLY_KIND,
        "ocr_performed": False,
        "reason_code": "audio_only_no_video_stream",
    }
    assert payload["clips"][0]["ocr_evidence"] == {
        "subtitle_text": "",
        "similarity": 0.0,
        "caption_count": 0,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload.update({"training_ready": True}),
            "training_ready must be false",
        ),
        (
            lambda payload: payload["clips"][0].update(
                {"audio_path": "../outside.wav"}
            ),
            "not contained",
        ),
        (
            lambda payload: payload["clips"][0].update({"audio_sha256": "0" * 64}),
            "binding does not match",
        ),
        (
            lambda payload: payload["clips"][1].update({"clip_id": "CLIP-001"}),
            "filename collision",
        ),
        (
            lambda payload: payload["clips"][0].update(
                {"audio_path": "clips//clip-001.wav"}
            ),
            "not contained",
        ),
    ],
)
def test_rejects_unsafe_or_mutable_queue(tmp_path, mutation, message):
    manifest_path = _queue(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if message == "not contained":
        outside = manifest_path.parent.parent / "outside.wav"
        _write_wav(outside)
        payload["clips"][0]["audio_sha256"] = _digest(outside)
        payload["clips"][0]["audio_size_bytes"] = outside.stat().st_size
    mutation(payload)
    _write_json(manifest_path, payload)

    with pytest.raises(MODULE.ReviewToolError, match=message):
        MODULE.QueueSnapshot.load(manifest_path)


def test_rejects_symbolic_linked_audio(tmp_path):
    manifest_path = _queue(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    original = manifest_path.parent / payload["clips"][0]["audio_path"]
    link = original.with_name("linked.wav")
    try:
        link.symlink_to(original)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    payload["clips"][0]["audio_path"] = "clips/linked.wav"
    _write_json(manifest_path, payload)

    with pytest.raises(MODULE.ReviewToolError, match="symbolic links"):
        MODULE.QueueSnapshot.load(manifest_path)


def test_decisions_are_separate_atomic_and_never_training_ready(tmp_path):
    manifest_path = _queue(tmp_path, clip_count=1)
    queue_hash = _digest(manifest_path)
    audio = manifest_path.parent / "clips" / "clip-001.wav"
    audio_hash = _digest(audio)
    decisions_path = tmp_path / "review-decisions.json"
    queue = MODULE.QueueSnapshot.load(manifest_path)
    store = MODULE.DecisionStore(decisions_path, queue)

    partial, decisions = store.record(_decision("clip-001"))

    assert partial["gates_satisfied"] is False
    assert decisions["clip-001"] == partial
    assert _digest(manifest_path) == queue_hash
    assert _digest(audio) == audio_hash
    assert not list(tmp_path.glob(".review-decisions.json-*.tmp"))
    payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    assert payload["training_ready"] is False
    assert payload["ready_for_local_training"] is False
    assert payload["runtime_promotion_allowed"] is False
    assert payload["statistics"]["all_review_gates_satisfied"] is False
    assert not (tmp_path / "train.jsonl").exists()

    complete, _ = store.record(_decision("clip-001", complete=True))
    assert complete["approved"] is True
    assert complete["gates_satisfied"] is True
    payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    assert payload["statistics"]["all_review_gates_satisfied"] is True
    assert payload["training_ready"] is False


def test_rejects_approval_without_all_explicit_gates(tmp_path):
    manifest_path = _queue(tmp_path, clip_count=1)
    queue = MODULE.QueueSnapshot.load(manifest_path)
    decisions_path = tmp_path / "review-decisions.json"
    store = MODULE.DecisionStore(decisions_path, queue)
    request = _decision("clip-001", complete=True)
    request["no_background_music"] = False

    with pytest.raises(MODULE.ReviewToolError, match="every review prerequisite"):
        store.record(request)

    assert not decisions_path.exists()


def test_rejects_decisions_inside_queue_or_bound_to_changed_queue(tmp_path):
    manifest_path = _queue(tmp_path, clip_count=1)
    queue = MODULE.QueueSnapshot.load(manifest_path)
    with pytest.raises(MODULE.ReviewToolError, match="outside the immutable queue"):
        MODULE.DecisionStore(manifest_path.parent / "decisions.json", queue)

    store = MODULE.DecisionStore(tmp_path / "decisions.json", queue)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["limitations"] = ["external mutation"]
    _write_json(manifest_path, payload)
    with pytest.raises(MODULE.ReviewToolError, match="changed after server startup"):
        store.record(_decision("clip-001"))


def _request(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    return (
        response.status,
        {key.lower(): value for key, value in response.getheaders()},
        payload,
    )


def test_loopback_http_auth_csrf_audio_whitelist_and_decision_api(tmp_path):
    manifest_path = _queue(tmp_path, clip_count=1)
    decisions_path = tmp_path / "decisions.json"
    access_token = "a" * 32
    csrf_token = "c" * 32
    server = MODULE.create_server(
        queue_path=manifest_path,
        decisions_path=decisions_path,
        port=0,
        access_token=access_token,
        csrf_token=csrf_token,
    )
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        status, _, _ = _request(connection, "GET", "/api/queue")
        assert status == 401

        status, headers, _ = _request(connection, "GET", f"/login?token={access_token}")
        assert status == 303
        assert headers["location"] == "/"
        assert access_token not in headers["location"]
        cookie = headers["set-cookie"].split(";", 1)[0]
        session_headers = {"Cookie": cookie}

        status, headers, html = _request(
            connection, "GET", "/", headers=session_headers
        )
        assert status == 200
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "Python" not in headers["server"]
        assert b"VoxCPM transcript review" in html
        assert access_token.encode() not in html

        status, _, raw_queue = _request(
            connection, "GET", "/api/queue", headers=session_headers
        )
        assert status == 200
        queue_payload = json.loads(raw_queue)
        assert queue_payload["clips"][0]["decision"]["approved"] is False

        status, headers, audio = _request(
            connection,
            "GET",
            "/api/audio/clip-001",
            headers={**session_headers, "Range": "bytes=0-15"},
        )
        assert status == 206
        assert headers["content-range"].startswith("bytes 0-15/")
        assert len(audio) == 16

        status, _, _ = _request(
            connection,
            "GET",
            "/api/audio/clip-001",
            headers={**session_headers, "Range": f"bytes={'9' * 21}-"},
        )
        assert status == 416

        status, _, _ = _request(
            connection,
            "GET",
            "/api/audio/%2e%2e%2fsecret.wav",
            headers=session_headers,
        )
        assert status == 404

        partial_body = json.dumps(_decision("clip-001")).encode()
        status, headers, _ = _request(
            connection,
            "POST",
            "/api/decisions",
            body=partial_body,
            headers={
                **session_headers,
                "Content-Type": "application/json",
                "Origin": server.origin,
            },
        )
        assert status == 403
        assert headers["connection"] == "close"
        assert not decisions_path.exists()

        status, _, response_body = _request(
            connection,
            "POST",
            "/api/decisions",
            body=partial_body,
            headers={
                **session_headers,
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-CSRF-Token": csrf_token,
            },
        )
        assert status == 200
        assert json.loads(response_body)["decision"]["gates_satisfied"] is False

        invalid = _decision("clip-001", complete=True)
        invalid["unknown"] = True
        invalid_body = json.dumps(invalid).encode()
        status, _, _ = _request(
            connection,
            "POST",
            "/api/decisions",
            body=invalid_body,
            headers={
                **session_headers,
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-CSRF-Token": csrf_token,
            },
        )
        assert status == 400

        complete_body = json.dumps(_decision("clip-001", complete=True)).encode()
        status, _, response_body = _request(
            connection,
            "POST",
            "/api/decisions",
            body=complete_body,
            headers={
                **session_headers,
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-CSRF-Token": csrf_token,
            },
        )
        assert status == 200
        response = json.loads(response_body)
        assert response["decision"]["gates_satisfied"] is True
        assert response["all_review_gates_satisfied"] is True
        stored = json.loads(decisions_path.read_text(encoding="utf-8"))
        assert stored["training_ready"] is False
        assert stored["statistics"]["all_review_gates_satisfied"] is True
        assert not (tmp_path / "train.jsonl").exists()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
