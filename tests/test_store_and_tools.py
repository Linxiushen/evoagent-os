from pathlib import Path

import pytest

from evoagent_runtime.models import RiskLevel, ToolCall
from evoagent_runtime.store import Store
from evoagent_runtime.tools import ToolContext, ToolPolicy, builtins


def test_fts_memory_and_event_ledger(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    store.ensure_session("s1", "api", "local")
    store.remember("s1", "production deployment is Friday", ["ops"])
    seq = store.event("test.recorded", {"ok": True}, session_id="s1")

    assert store.search_memory("deployment")[0]["session_id"] == "s1"
    assert store.events(seq - 1)[0]["payload"] == {"ok": True}
    store.close()


@pytest.mark.asyncio
async def test_workspace_containment(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    store.ensure_session("s1", "api", "local")
    registry = builtins(ToolPolicy(require_approval=set()))
    context = ToolContext(store, "s1", "r1", tmp_path / "workspace", set())
    context.workspace.mkdir()

    with pytest.raises(ValueError, match="escapes workspace"):
        await registry.execute(
            ToolCall(name="workspace.write", arguments={"path": "../escape.txt", "content": "x"}),
            context,
        )
    store.close()


def test_default_policy_requires_operator_for_mutations() -> None:
    registry = builtins()
    assert registry.policy.decide(registry.get("clock.now")).action == "execute"
    assert registry.policy.decide(registry.get("workspace.read")).action == "approve"
    assert registry.get("workspace.write").risk == RiskLevel.HIGH
