from __future__ import annotations

import argparse
import asyncio

import uvicorn

from harnesslab.adapters import DeepSeekHarnessProbe
from harnesslab.api import build_runtime
from harnesslab.conformance import run_conformance


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnesslab",
        description="Run, inspect, and test agent harness adapters.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Start the trace console and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4318)
    serve.add_argument("--reload", action="store_true")

    run = commands.add_parser("run", help="Run one task and print its trace")
    run.add_argument("task")
    run.add_argument("--adapter", default="demo")
    run.add_argument("--max-turns", type=int, default=6)

    check = commands.add_parser("check", help="Run the harness conformance matrix")
    check.add_argument("--adapter", default="demo")

    probe = commands.add_parser(
        "probe",
        help="Probe a future harness capability document without assuming its protocol",
    )
    probe.add_argument("base_url")
    probe.add_argument("--token")
    return parser


async def run_task(task: str, adapter: str, max_turns: int) -> None:
    runtime = build_runtime()
    record = await runtime.run(task, adapter_name=adapter, max_turns=max_turns)
    print(record.model_dump_json(indent=2))


async def run_check(adapter: str) -> None:
    runtime = build_runtime()
    report = await run_conformance(runtime, adapter)
    print(report.model_dump_json(indent=2))
    if report.passed != report.total:
        raise SystemExit(1)


async def run_probe(base_url: str, token: str | None) -> None:
    document = await DeepSeekHarnessProbe(base_url, token).discover()
    print(document.model_dump_json(indent=2))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        uvicorn.run("harnesslab.api:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "run":
        asyncio.run(run_task(args.task, args.adapter, args.max_turns))
    elif args.command == "check":
        asyncio.run(run_check(args.adapter))
    elif args.command == "probe":
        asyncio.run(run_probe(args.base_url, args.token))


if __name__ == "__main__":
    main()
