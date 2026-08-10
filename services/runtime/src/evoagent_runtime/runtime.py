from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from .models import ChatMessage, Envelope, RunStatus, RunView, ToolCall
from .providers import OfflineProvider, Provider
from .store import Store
from .tools import ToolContext, ToolPolicy, ToolRegistry, builtins, serialize_result


class AgentRuntime:
    def __init__(
        self,
        store: Store,
        workspace: Path,
        provider: Provider | None = None,
        tools: ToolRegistry | None = None,
        allowed_hosts: set[str] | None = None,
        max_tool_steps: int = 8,
    ) -> None:
        self.store = store
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.provider = provider or OfflineProvider()
        self.tools = tools or builtins(ToolPolicy())
        self.allowed_hosts = allowed_hosts or set()
        self.max_tool_steps = max_tool_steps
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def session_id(envelope: Envelope) -> str:
        if envelope.session_id:
            return envelope.session_id
        digest = hashlib.sha256(f"{envelope.channel}:{envelope.peer_id}".encode()).hexdigest()[:20]
        return f"session_{digest}"

    def accept(self, envelope: Envelope) -> tuple[str, str, int, str]:
        session_id = self.session_id(envelope)
        self.store.ensure_session(session_id, envelope.channel, envelope.peer_id)
        prompt_version, prompt = self.store.active_prompt()
        run_id = self.store.create_run(session_id, envelope.text, prompt_version)
        self.store.add_message(session_id, "user", envelope.text)
        self.store.event("run.accepted", {"channel": envelope.channel}, run_id, session_id)
        return run_id, session_id, prompt_version, prompt

    async def submit(self, envelope: Envelope) -> str:
        run_id, session_id, _, prompt = self.accept(envelope)
        task = asyncio.create_task(self._execute(run_id, session_id, prompt, envelope.text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def run_once(self, envelope: Envelope) -> RunView:
        run_id, session_id, _, prompt = self.accept(envelope)
        await self._execute(run_id, session_id, prompt, envelope.text)
        return self.run_view(run_id)

    def run_view(self, run_id: str) -> RunView:
        row = self.store.get_run(run_id)
        if row is None:
            raise ValueError("Run not found")
        return RunView(**row)

    def _context(self, run_id: str, session_id: str) -> ToolContext:
        return ToolContext(
            store=self.store,
            session_id=session_id,
            run_id=run_id,
            workspace=self.workspace,
            allowed_hosts=self.allowed_hosts,
        )

    def _messages(self, session_id: str, prompt: str, query: str) -> list[ChatMessage]:
        memory = self.store.search_memory(query, limit=5)
        memory_text = "\n".join(f"- {item['text']}" for item in memory)
        system = prompt
        if memory_text:
            system += f"\n\nRelevant operator memory:\n{memory_text}"
        messages = [ChatMessage(role="system", content=system)]
        for item in self.store.history(session_id, limit=40):
            messages.append(
                ChatMessage(
                    role=item["role"],
                    content=item["content"],
                    name=item["name"],
                    tool_call_id=item["tool_call_id"],
                )
            )
        return messages

    async def _execute(self, run_id: str, session_id: str, prompt: str, query: str) -> None:
        self.store.update_run(run_id, RunStatus.RUNNING)
        self.store.event("run.started", {}, run_id, session_id)
        try:
            await self._drive(run_id, session_id, prompt, self._messages(session_id, prompt, query))
        except Exception as exc:  # noqa: BLE001 - persist every provider/tool boundary failure.
            self.store.update_run(run_id, RunStatus.FAILED, error=str(exc))
            self.store.event("run.failed", {"error": str(exc)}, run_id, session_id)

    async def _drive(
        self, run_id: str, session_id: str, prompt: str, messages: list[ChatMessage]
    ) -> None:
        usage = {"input_tokens": 0, "output_tokens": 0}
        for step in range(self.max_tool_steps + 1):
            turn = await self.provider.complete(messages, self.tools.schemas())
            usage["input_tokens"] += turn.input_tokens
            usage["output_tokens"] += turn.output_tokens
            self.store.event(
                "model.completed",
                {"model": turn.model, "tool_calls": len(turn.tool_calls), "step": step},
                run_id,
                session_id,
            )
            if turn.text:
                self.store.add_message(session_id, "assistant", turn.text)
                messages.append(ChatMessage(role="assistant", content=turn.text))
            if not turn.tool_calls:
                self.store.update_run(
                    run_id,
                    RunStatus.COMPLETED,
                    output_text=turn.text,
                    usage_json=json.dumps(usage),
                )
                self.store.event("run.completed", {"usage": usage}, run_id, session_id)
                return
            for call in turn.tool_calls:
                tool = self.tools.get(call.name)
                decision = self.tools.policy.decide(tool)
                self.store.event(
                    "tool.requested",
                    {"tool": call.name, "risk": tool.risk.value, "decision": decision.action},
                    run_id,
                    session_id,
                )
                if decision.action == "deny":
                    result = {"error": decision.reason}
                elif decision.action == "approve":
                    approval_id = self.store.create_approval(
                        run_id, session_id, call.name, call.arguments, decision.reason
                    )
                    self.store.update_run(run_id, RunStatus.AWAITING_APPROVAL)
                    self.store.event(
                        "approval.requested",
                        {"approval_id": approval_id, "tool": call.name, "reason": decision.reason},
                        run_id,
                        session_id,
                    )
                    return
                else:
                    result = await self.tools.execute(call, self._context(run_id, session_id))
                serialized = serialize_result(result)
                self.store.add_message(session_id, "tool", serialized, call.name, call.id)
                messages.append(
                    ChatMessage(
                        role="tool", content=serialized, name=call.name, tool_call_id=call.id
                    )
                )
                self.store.event(
                    "tool.completed", {"tool": call.name, "result": result}, run_id, session_id
                )
        raise RuntimeError(f"Tool step budget exceeded ({self.max_tool_steps})")

    async def decide_approval(self, approval_id: str, approved: bool, actor: str) -> RunView:
        approval = self.store.approval(approval_id)
        if approval is None:
            raise ValueError("Approval not found")
        if approval["status"] != "pending":
            raise ValueError("Approval has already been decided")
        self.store.decide_approval(approval_id, approved, actor)
        run = self.store.get_run(approval["run_id"])
        if run is None:
            raise ValueError("Run not found")
        call = ToolCall(name=approval["tool_name"], arguments=json.loads(approval["args_json"]))
        if approved:
            result = await self.tools.execute(call, self._context(run["run_id"], run["session_id"]))
        else:
            result = {"error": f"Operator {actor} denied this tool call"}
        serialized = serialize_result(result)
        self.store.add_message(run["session_id"], "tool", serialized, call.name, call.id)
        self.store.event(
            "approval.decided",
            {"approval_id": approval_id, "approved": approved, "actor": actor},
            run["run_id"],
            run["session_id"],
        )
        self.store.update_run(run["run_id"], RunStatus.RUNNING)
        prompt = self.store.prompt(int(run["prompt_version"]))
        await self._drive(
            run["run_id"],
            run["session_id"],
            prompt,
            self._messages(run["session_id"], prompt, run["input_text"]),
        )
        return self.run_view(run["run_id"])
