import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from echoweave.app import MAX_MIC_PAYLOAD_BYTES, create_app
from echoweave.config import Settings
from echoweave.persona import sign_manifest
from echoweave.protocol import PacketKind, pack_packet

TEST_ACCESS_TOKEN = "test-access-token-with-at-least-32-bytes"
TEST_SIGNING_KEY = "test-signing-key-with-at-least-32-bytes"


def _start_demo(socket) -> None:
    hello = socket.receive_json()
    assert hello["type"] == "session.hello"
    socket.send_json(
        {
            "type": "start",
            "persona_id": "demo",
            "access_token": TEST_ACCESS_TOKEN,
            "ai_disclosure_ack": True,
        }
    )
    ready_types = {socket.receive_json()["type"], socket.receive_json()["type"]}
    assert ready_types == {"session.state", "session.ready"}


def _write_external_fictional_persona(root) -> None:
    persona_dir = root / "fictional"
    persona_dir.mkdir()
    skill_path = persona_dir / "SKILL.md"
    skill_path.write_text("# Reviewed fictional perspective", encoding="utf-8")
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": 1,
        "manifest_revision": 1,
        "persona_id": "fictional",
        "consent_id": "fictional-test-consent",
        "subject_display_name": "Fictional Test",
        "profile_class": "fictional_original",
        "asset_rights_record_id": "test-original-art-record",
        "consent_granted": True,
        "consent_withdrawn": False,
        "consent_scope": [
            "interactive_conversation",
            "persona_profile",
            "voice_clone",
            "avatar_animation",
        ],
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "valid_until": (now + timedelta(days=1)).isoformat(),
        "nuwa_skill": "SKILL.md",
        "reference_hashes": {
            "SKILL.md": hashlib.sha256(skill_path.read_bytes()).hexdigest()
        },
    }
    manifest_path = persona_dir / "consent.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sign_manifest(manifest_path, TEST_SIGNING_KEY)


def test_health_does_not_expose_secrets(tmp_path):
    app = create_app(
        Settings(
            persona_root=tmp_path,
            deepseek_api_key="must-not-leak",
            consent_signing_key="also-private-but-at-least-32-bytes",
            consent_state_path=tmp_path / "consent-state.json",
        )
    )
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert set(payload) == {"ok", "service", "version"}
    assert "must-not-leak" not in response.text
    assert "also-private-but-at-least-32-bytes" not in response.text


def test_websocket_text_turn(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        _start_demo(socket)

        socket.send_json({"type": "text", "text": "端到端测试"})
        received = []
        for _ in range(100):
            event = socket.receive_json()
            received.append(event["type"])
            if event["type"] == "assistant.final":
                break
        assert "assistant.delta" in received
        assert "tts.browser" in received
        assert "avatar.segment" in received
        assert received[-1] == "assistant.final"


def test_external_persona_session_uses_its_immutable_grant(tmp_path):
    _write_external_fictional_persona(tmp_path)
    app = create_app(
        Settings(
            persona_root=tmp_path,
            allowed_personas=("fictional",),
            access_token=TEST_ACCESS_TOKEN,
            consent_signing_key=TEST_SIGNING_KEY,
            consent_state_path=tmp_path / "consent-state.json",
        )
    )
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "fictional",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
            }
        )
        ready_types = {socket.receive_json()["type"], socket.receive_json()["type"]}
        assert ready_types == {"session.state", "session.ready"}
        socket.send_json({"type": "text", "text": "grant wiring"})
        error_codes = []
        for _ in range(100):
            event = socket.receive_json()
            if event["type"] == "error":
                error_codes.append(event["code"])
            if event["type"] == "assistant.final":
                break
        assert "authorization_revoked" not in error_codes


def test_websocket_rejects_non_object_without_disconnect(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(["not", "a", "control", "object"])
        error = socket.receive_json()
        assert error["code"] == "invalid_control_json"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
            }
        )
        ready_types = {socket.receive_json()["type"], socket.receive_json()["type"]}
        assert ready_types == {"session.state", "session.ready"}


def test_disclosure_acknowledgement_requires_json_true(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": "true",
            }
        )
        error = socket.receive_json()
        assert error["code"] == "disclosure_not_acknowledged"


def test_local_origin_tracks_non_default_port(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        port=9877,
        access_token=TEST_ACCESS_TOKEN,
    )
    app = create_app(settings)
    with TestClient(app).websocket_connect(
        "/ws",
        headers={"origin": "http://127.0.0.1:9877"},
    ) as socket:
        assert socket.receive_json()["type"] == "session.hello"


def test_tokenless_websocket_rejects_non_loopback_peer(tmp_path):
    app = create_app(Settings(persona_root=tmp_path))
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        TestClient(app).websocket_connect("/ws"),
    ):
        pass
    assert caught.value.code == 1008


def test_oversized_microphone_frame_is_rejected_and_closed(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        _start_demo(socket)
        socket.send_bytes(
            pack_packet(
                PacketKind.MIC_PCM16,
                0,
                0,
                b"\x00" * (MAX_MIC_PAYLOAD_BYTES + 2),
            )
        )
        error = socket.receive_json()
        assert error["code"] == "invalid_mic_frame"
        close_message = socket.receive()
        assert close_message["type"] == "websocket.close"
        assert close_message["code"] == 1008


def test_microphone_audio_is_limited_to_realtime_rate(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        _start_demo(socket)
        error = None
        for index in range(20):
            socket.send_bytes(
                pack_packet(
                    PacketKind.MIC_PCM16,
                    0,
                    index * 250,
                    b"\x00" * MAX_MIC_PAYLOAD_BYTES,
                )
            )
            event = socket.receive_json()
            if event["type"] == "error":
                error = event
                break
            assert event["type"] == "vad.level"
        assert error is not None
        assert error["code"] == "mic_rate_exceeded"
