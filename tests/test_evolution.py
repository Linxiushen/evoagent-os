from pathlib import Path

import pytest

from evoagent_runtime.evolution import EvolutionEngine
from evoagent_runtime.models import Envelope, EvolutionScenario
from evoagent_runtime.providers import OfflineProvider
from evoagent_runtime.runtime import AgentRuntime
from evoagent_runtime.store import Store


@pytest.mark.asyncio
async def test_candidate_must_pass_gate_before_promotion(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    runtime = AgentRuntime(store, tmp_path / "workspace", OfflineProvider())
    run = await runtime.run_once(Envelope(text="summarize deployment"))
    store.add_feedback(run.run_id, -1, "Give verification steps")
    engine = EvolutionEngine(store, OfflineProvider())
    candidate = engine.propose()

    with pytest.raises(ValueError, match="passed"):
        engine.promote(candidate.candidate_id)

    result = await engine.evaluate(
        candidate.candidate_id,
        [EvolutionScenario(input="status", required=["received"], forbidden=["password"])],
    )
    assert result.status == "passed"
    assert engine.promote(candidate.candidate_id) == 2
    assert store.active_prompt()[0] == 2
    store.close()
