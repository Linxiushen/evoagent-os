from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from .api import create_app
from .models import Completion, NodeSpec, WorkerRegistration, WorkflowSpec
from .orchestrator import Orchestrator
from .store import Store


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="evoagent-fleet")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--state-dir", type=Path, default=Path(".fleet"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8833)
    demo_command = commands.add_parser("demo")
    demo_command.add_argument("--state-dir", type=Path, default=Path(".fleet"))
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--state-dir", type=Path, default=Path(".fleet"))
    return root


def demo(state_dir: Path) -> dict[str, object]:
    store = Store(state_dir / "fleet.sqlite")
    fleet = Orchestrator(store, state_dir / "artifacts")
    workflow_id = fleet.submit(
        WorkflowSpec(
            name="release-readiness",
            nodes=[
                NodeSpec(
                    id="research", objective="Collect release evidence", capabilities=["research"]
                ),
                NodeSpec(
                    id="review",
                    objective="Review evidence",
                    depends_on=["research"],
                    capabilities=["review"],
                ),
                NodeSpec(
                    id="publish",
                    objective="Publish approved report",
                    depends_on=["review"],
                    capabilities=["publish"],
                    approval_required=True,
                ),
            ],
        )
    )
    for worker_id, capability in (("researcher-1", "research"), ("reviewer-1", "review")):
        fleet.register(
            WorkerRegistration(worker_id=worker_id, capabilities=[capability], pool=capability)
        )
        lease = fleet.claim(worker_id)
        if lease:
            fleet.complete(
                worker_id,
                lease["lease_token"],
                Completion(
                    output={"result": f"{capability} complete"},
                    artifacts={f"{capability}.md": "verified"},
                ),
            )
    view = fleet.view(workflow_id)
    store.close()
    return view


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "serve":
        uvicorn.run(
            create_app(args.state_dir / "fleet.sqlite", args.state_dir / "artifacts"),
            host=args.host,
            port=args.port,
        )
    elif args.command == "demo":
        print(json.dumps(demo(args.state_dir), indent=2))
    elif args.command == "doctor":
        print(
            json.dumps(
                {
                    "state_dir": str(args.state_dir.resolve()),
                    "database": str((args.state_dir / "fleet.sqlite").resolve()),
                    "artifact_root": str((args.state_dir / "artifacts").resolve()),
                },
                indent=2,
            )
        )
    return 0
