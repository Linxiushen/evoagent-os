from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from .api import create_app
from .evolution import EvolutionEngine
from .models import Envelope, EvolutionScenario
from .providers import OfflineProvider
from .runtime import AgentRuntime
from .store import Store


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="evoagent-runtime")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the HTTP/WebSocket Gateway and Control UI")
    serve.add_argument("--state-dir", type=Path, default=Path(".evoagent"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8811)
    serve.add_argument("--provider", choices=("offline", "openai"), default="offline")
    demo = commands.add_parser("demo", help="Run an offline memory, approval, and evolution demo")
    demo.add_argument("--state-dir", type=Path, default=Path(".evoagent"))
    demo.add_argument("--reset", action="store_true")
    doctor = commands.add_parser("doctor", help="Print configuration and runtime checks")
    doctor.add_argument("--state-dir", type=Path, default=Path(".evoagent"))
    return root


async def demo(state_dir: Path) -> None:
    store = Store(state_dir / "runtime.sqlite")
    runtime = AgentRuntime(store, state_dir / "workspace", OfflineProvider())
    first = await runtime.run_once(
        Envelope(text="remember: my deployment window is Friday 20:00 UTC")
    )
    second = await runtime.run_once(Envelope(text="recall: deployment window"))
    risky = await runtime.run_once(Envelope(text="write: release.txt=ship after CI"))
    approval = store.pending_approvals()[0]
    resumed = await runtime.decide_approval(approval["approval_id"], True, "demo-operator")
    store.add_feedback(second.run_id, -1, "Always give explicit verification steps")
    engine = EvolutionEngine(store, runtime.provider)
    candidate = engine.propose()
    evaluated = await engine.evaluate(
        candidate.candidate_id,
        [
            EvolutionScenario(
                input="Summarize this deployment", required=["received"], forbidden=["password"]
            )
        ],
    )
    version = engine.promote(candidate.candidate_id) if evaluated.status == "passed" else None
    print(
        json.dumps(
            {
                "memory_run": first.model_dump(),
                "recall_run": second.model_dump(),
                "approval_before": risky.status,
                "approval_after": resumed.status,
                "candidate": evaluated.model_dump(),
                "promoted_prompt_version": version,
                "database": str(store.path),
            },
            indent=2,
            default=str,
        )
    )
    store.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state_dir: Path = args.state_dir
    if args.command == "serve":
        app = create_app(
            state_dir / "runtime.sqlite",
            state_dir / "workspace",
            args.provider,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "demo":
        if args.reset and state_dir.exists():
            raise SystemExit(
                "Refusing to delete an existing state directory; choose a fresh --state-dir"
            )
        state_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(demo(state_dir))
        return 0
    if args.command == "doctor":
        writable_target = state_dir if state_dir.exists() else state_dir.parent
        checks = {
            "state_dir": str(state_dir.resolve()),
            "state_writable": os.access(writable_target.resolve(), os.W_OK),
            "provider": os.getenv("EVOAGENT_PROVIDER", "offline"),
            "model_key_present": bool(os.getenv("EVOAGENT_API_KEY") or os.getenv("OPENAI_API_KEY")),
            "gateway_token_configured": bool(os.getenv("EVOAGENT_GATEWAY_TOKEN")),
            "http_allowlist": [
                item for item in os.getenv("EVOAGENT_HTTP_ALLOWLIST", "").split(",") if item
            ],
        }
        print(json.dumps(checks, indent=2))
        return 0
    return 2
