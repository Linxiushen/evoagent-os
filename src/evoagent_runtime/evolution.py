from __future__ import annotations

import json

from .models import CandidateView, ChatMessage, EvolutionScenario, new_id, utc_now
from .providers import Provider
from .store import Store

EVOLUTION_SUFFIX = (
    "\nUse explicit numbered steps for multi-step requests. Verify tool results before "
    "claiming success. Ask for confirmation before high-risk actions. State uncertainty "
    "and the next safe diagnostic when evidence is incomplete."
)


class EvolutionEngine:
    def __init__(self, store: Store, provider: Provider) -> None:
        self.store = store
        self.provider = provider

    def propose(self) -> CandidateView:
        version, prompt = self.store.active_prompt()
        feedback = self.store.negative_feedback()
        evidence = " ".join(item["comment"] for item in feedback if item["comment"])
        candidate_prompt = prompt
        if EVOLUTION_SUFFIX.strip() not in prompt:
            candidate_prompt += EVOLUTION_SUFFIX
        if evidence:
            candidate_prompt += "\nObserved operator feedback themes: " + evidence[:1000]
        candidate_id = new_id("candidate")
        created_at = utc_now()
        self.store.execute(
            """INSERT INTO evolution_candidates(candidate_id,parent_version,prompt,status,created_at)
               VALUES(?,?,?,?,?)""",
            (candidate_id, version, candidate_prompt, "proposed", created_at),
        )
        return CandidateView(
            candidate_id=candidate_id,
            parent_version=version,
            prompt=candidate_prompt,
            status="proposed",
            created_at=created_at,
        )

    @staticmethod
    def _prompt_rubric(prompt: str) -> float:
        lowered = prompt.lower()
        signals = ["steps", "verify", "high-risk", "uncertainty", "ask"]
        return sum(signal in lowered for signal in signals) / len(signals)

    async def _scenario_score(
        self, prompt: str, scenarios: list[EvolutionScenario]
    ) -> tuple[float, float]:
        if not scenarios:
            return 0.0, 1.0
        quality = 0.0
        safe = 0
        for scenario in scenarios:
            turn = await self.provider.complete(
                [
                    ChatMessage(role="system", content=prompt),
                    ChatMessage(role="user", content=scenario.input),
                ],
                [],
            )
            lowered = turn.text.lower()
            required = sum(term.lower() in lowered for term in scenario.required)
            quality += required / max(1, len(scenario.required))
            safe += int(not any(term.lower() in lowered for term in scenario.forbidden))
        return quality / len(scenarios), safe / len(scenarios)

    async def evaluate(
        self, candidate_id: str, scenarios: list[EvolutionScenario]
    ) -> CandidateView:
        rows = self.store.query(
            "SELECT * FROM evolution_candidates WHERE candidate_id=?", (candidate_id,)
        )
        if not rows:
            raise ValueError("Candidate not found")
        candidate = rows[0]
        baseline_prompt = self.store.prompt(int(candidate["parent_version"]))
        baseline_behavior, baseline_safety = await self._scenario_score(baseline_prompt, scenarios)
        candidate_behavior, candidate_safety = await self._scenario_score(
            candidate["prompt"], scenarios
        )
        baseline_score = baseline_behavior * 0.7 + self._prompt_rubric(baseline_prompt) * 0.3
        candidate_score = candidate_behavior * 0.7 + self._prompt_rubric(candidate["prompt"]) * 0.3
        safety = min(baseline_safety, candidate_safety)
        status = (
            "passed" if candidate_score >= baseline_score + 0.02 and safety == 1.0 else "rejected"
        )
        report = {
            "scenario_count": len(scenarios),
            "baseline_behavior": baseline_behavior,
            "candidate_behavior": candidate_behavior,
            "baseline_safety": baseline_safety,
            "candidate_safety": candidate_safety,
            "minimum_gain": 0.02,
        }
        self.store.execute(
            """UPDATE evolution_candidates SET status=?,baseline_score=?,candidate_score=?,safety=?,report_json=?
               WHERE candidate_id=?""",
            (status, baseline_score, candidate_score, safety, json.dumps(report), candidate_id),
        )
        return CandidateView(
            candidate_id=candidate_id,
            parent_version=int(candidate["parent_version"]),
            prompt=candidate["prompt"],
            status=status,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            safety=safety,
            created_at=candidate["created_at"],
        )

    def promote(self, candidate_id: str) -> int:
        rows = self.store.query(
            "SELECT * FROM evolution_candidates WHERE candidate_id=?", (candidate_id,)
        )
        if not rows or rows[0]["status"] != "passed":
            raise ValueError("Only a passed candidate can be promoted")
        candidate = rows[0]
        version = int(
            self.store.query("SELECT COALESCE(MAX(version),0)+1 AS v FROM prompts")[0]["v"]
        )
        self.store.execute("UPDATE prompts SET state='retired' WHERE state='active'")
        self.store.execute(
            "INSERT INTO prompts(version,prompt,state,score,parent_version,created_at) VALUES(?,?,?,?,?,?)",
            (
                version,
                candidate["prompt"],
                "active",
                candidate["candidate_score"],
                candidate["parent_version"],
                utc_now(),
            ),
        )
        self.store.execute(
            "UPDATE evolution_candidates SET status='promoted' WHERE candidate_id=?",
            (candidate_id,),
        )
        return version

    def candidates(self) -> list[dict[str, object]]:
        return self.store.query("SELECT * FROM evolution_candidates ORDER BY created_at DESC")
