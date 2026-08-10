from pathlib import Path

from fastapi.testclient import TestClient

from evoagent_runtime.api import create_app


def test_http_gateway_auth_and_wait(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "state.sqlite",
        tmp_path / "workspace",
        provider_mode="offline",
        gateway_token="secret",
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/runs").status_code == 401
        response = client.post(
            "/v1/messages/wait",
            headers={"Authorization": "Bearer secret"},
            json={"text": "hello"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"


def test_websocket_protocol(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state.sqlite", tmp_path / "workspace", gateway_token="token")
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "connect", "token": "token"})
        assert socket.receive_json()["type"] == "hello-ok"
        socket.send_json({"id": "1", "method": "agent", "params": {"text": "hello"}})
        response = socket.receive_json()
        assert response["ok"] is True
        assert response["payload"]["run_id"].startswith("run_")
