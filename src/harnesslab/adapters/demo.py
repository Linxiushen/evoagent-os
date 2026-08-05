from __future__ import annotations

from harnesslab.models import AdapterTurn, Message, ToolCall, ToolSpec


class DemoAdapter:
    """Deterministic adapter used by the dashboard, tests, and offline demos."""

    name = "demo"

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AdapterTurn:
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            return AdapterTurn(
                text="I will locate the relevant policy path before judging the change.",
                tool_calls=[
                    ToolCall(
                        id="call_search_1",
                        name="search_repository",
                        arguments={"query": "authorization before checkout mutation"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 182, "output_tokens": 31},
            )
        if len(tool_messages) == 1:
            return AdapterTurn(
                text="The search isolated the checkout policy. I will inspect the mutation order.",
                tool_calls=[
                    ToolCall(
                        id="call_inspect_1",
                        name="inspect_change",
                        arguments={
                            "path": "src/checkout/policy.py",
                            "focus": "authorization and rollback ordering",
                        },
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 347, "output_tokens": 38},
            )
        return AdapterTurn(
            text=(
                "High-risk regression found in `src/checkout/policy.py:42`: the change mutates "
                "checkout state before authorization completes. A rejected request can therefore "
                "leave partial state behind. Move the authorization guard ahead of the mutation "
                "and add a rollback assertion to `tests/test_checkout_policy.py`."
            ),
            finish_reason="stop",
            usage={"input_tokens": 524, "output_tokens": 74},
        )

