from pathlib import Path

import pytest

from evoagent_runtime.models import Envelope, RunStatus
from evoagent_runtime.providers import OfflineProvider
from evoagent_runtime.runtime import AgentRuntime
from evoagent_runtime.store import Store


@pytest.fixture
def store(tmp_path: Path):
    value = Store(tmp_path / "state.sqlite")
    yield value
    value.close()


@pytest.mark.asyncio
async def test_memory_round_trip_and_stable_session(store: Store, tmp_path: Path) -> None:
    runtime = AgentRuntime(store, tmp_path / "workspace", OfflineProvider())
    remembered = await runtime.run_once(Envelope(peer_id="alice", text="remember: deploy Friday"))
    recalled = await runtime.run_once(Envelope(peer_id="alice", text="recall: deploy"))

    assert remembered.status == RunStatus.COMPLETED
    assert recalled.status == RunStatus.COMPLETED
    assert "deploy Friday" in (recalled.output_text or "")
    assert remembered.session_id == recalled.session_id


@pytest.mark.asyncio
async def test_high_risk_tool_pauses_and_resumes(store: Store, tmp_path: Path) -> None:
    runtime = AgentRuntime(store, tmp_path / "workspace", OfflineProvider())
    waiting = await runtime.run_once(Envelope(text="write: release.txt=ship after CI"))

    assert waiting.status == RunStatus.AWAITING_APPROVAL
    approval = store.pending_approvals()[0]
    completed = await runtime.decide_approval(approval["approval_id"], True, "reviewer")

    assert completed.status == RunStatus.COMPLETED
    assert (tmp_path / "workspace" / "release.txt").read_text() == "ship after CI"
    assert store.approval(approval["approval_id"])["actor"] == "reviewer"


@pytest.mark.asyncio
async def test_denied_approval_does_not_write(store: Store, tmp_path: Path) -> None:
    runtime = AgentRuntime(store, tmp_path / "workspace", OfflineProvider())
    waiting = await runtime.run_once(Envelope(text="write: denied.txt=no"))
    approval = store.pending_approvals()[0]
    completed = await runtime.decide_approval(approval["approval_id"], False, "reviewer")

    assert waiting.status == RunStatus.AWAITING_APPROVAL
    assert completed.status == RunStatus.COMPLETED
    assert not (tmp_path / "workspace" / "denied.txt").exists()
    assert "denied" in (completed.output_text or "").lower()
