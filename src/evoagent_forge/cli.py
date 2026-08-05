from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import uvicorn

from .api import create_app
from .evolution import evaluate, write_evolution_proposal
from .package import build
from .registry import Registry
from .scanner import blocking, scan
from .signing import generate, sign, verify
from .skill import load_skill, scaffold


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="evoagent-forge")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("path", type=Path)
    init.add_argument("--name", required=True)
    for name in ("validate", "scan", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
    package = commands.add_parser("package")
    package.add_argument("path", type=Path)
    package.add_argument("--output", type=Path)
    keygen = commands.add_parser("keygen")
    keygen.add_argument("--private", type=Path, default=Path("forge-private.pem"))
    keygen.add_argument("--public", type=Path, default=Path("forge-public.pem"))
    signing = commands.add_parser("sign")
    signing.add_argument("artifact", type=Path)
    signing.add_argument("--key", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("artifact", type=Path)
    check.add_argument("--signature", type=Path, required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("path", type=Path)
    publish.add_argument("artifact", type=Path)
    publish.add_argument("--registry", type=Path, default=Path(".forge-registry"))
    publish.add_argument("--signature", type=Path)
    search = commands.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--registry", type=Path, default=Path(".forge-registry"))
    evolve = commands.add_parser("evolve")
    evolve.add_argument("path", type=Path)
    evolve.add_argument("--feedback", action="append", default=[])
    serve = commands.add_parser("serve")
    serve.add_argument("--registry", type=Path, default=Path(".forge-registry"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8822)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        print(scaffold(args.path, args.name))
        return 0
    if args.command in {"validate", "scan", "evaluate", "package", "publish", "evolve"}:
        manifest, _ = load_skill(args.path)
    if args.command == "validate":
        print(json.dumps(manifest.as_dict(), indent=2))
    elif args.command == "scan":
        findings = scan(args.path, manifest)
        print(json.dumps([item.as_dict() for item in findings], indent=2))
        return int(blocking(findings))
    elif args.command == "evaluate":
        print(json.dumps(asdict(evaluate(args.path, manifest)), indent=2))
    elif args.command == "package":
        findings = scan(args.path, manifest)
        if blocking(findings):
            raise SystemExit("Packaging blocked by high/critical scanner findings")
        output = args.output or Path(f"dist/{manifest.name}-{manifest.version}.evoskill")
        artifact, digest = build(args.path, manifest, output)
        print(json.dumps({"artifact": str(artifact), "sha256": digest}, indent=2))
    elif args.command == "keygen":
        print(generate(args.private, args.public))
    elif args.command == "sign":
        output = args.artifact.with_suffix(args.artifact.suffix + ".sig.json")
        print(json.dumps(sign(args.artifact, args.key, output), indent=2))
    elif args.command == "verify":
        valid = verify(args.artifact, args.signature)
        print(json.dumps({"valid": valid}))
        return int(not valid)
    elif args.command == "publish":
        registry = Registry(args.registry)
        print(json.dumps(registry.publish(manifest, args.artifact, args.signature), indent=2))
        registry.close()
    elif args.command == "search":
        registry = Registry(args.registry)
        print(json.dumps(registry.search(args.query), indent=2))
        registry.close()
    elif args.command == "evolve":
        baseline = evaluate(args.path, manifest)
        print(
            write_evolution_proposal(
                args.path, baseline, args.feedback, Path("evolution-proposal.json")
            )
        )
    elif args.command == "serve":
        uvicorn.run(create_app(args.registry), host=args.host, port=args.port)
    return 0
