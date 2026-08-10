from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from .models import PUBLIC_MODELS


def schema_bundle() -> dict[str, Any]:
    """Return one JSON Schema document with references for all public models."""

    inputs = [(model, "validation") for model in PUBLIC_MODELS]
    references, definitions = models_json_schema(
        inputs,
        title="EvoAgent OS Contracts",
        description="Versioned request, resource, event, and overview contracts.",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **definitions,
        "x-contract-version": "0.1.0",
        "models": {model.__name__: references[(model, "validation")] for model in PUBLIC_MODELS},
    }


def export_schema(destination: str | Path, *, indent: int = 2) -> None:
    rendered = json.dumps(schema_bundle(), indent=indent, sort_keys=True, ensure_ascii=True) + "\n"
    if str(destination) == "-":
        sys.stdout.write(rendered)
        return

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the EvoAgent contract JSON Schema")
    parser.add_argument("destination", nargs="?", default="-", help="output path, or - for stdout")
    parser.add_argument("--indent", type=int, default=2, choices=range(9))
    args = parser.parse_args(argv)
    export_schema(args.destination, indent=args.indent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
