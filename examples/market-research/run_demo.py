"""Exercise Fleet's workflow contract without calling a model or the network."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def request_json(base_url: str, method: str, path: str, body: object | None = None) -> Any:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Fleet URL must be an absolute HTTP(S) URL")
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(  # noqa: S310 - scheme is restricted above.
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is operator supplied.
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach Fleet at {base_url}: {exc.reason}") from exc


def register(base_url: str, worker_id: str, capability: str) -> None:
    request_json(
        base_url,
        "POST",
        "/v1/workers",
        {
            "worker_id": worker_id,
            "capabilities": [capability],
            "pool": capability,
            "max_concurrency": 1,
            "metadata": {"demo": True, "executes_live_research": False},
        },
    )


def claim_and_complete(
    base_url: str,
    worker_id: str,
    expected_workflow: str,
    expected_node: str,
    output: dict[str, Any],
    artifact_name: str,
    artifact_content: str,
) -> dict[str, Any]:
    lease = request_json(base_url, "POST", "/v1/claims", {"worker_id": worker_id}).get("lease")
    if lease is None:
        raise RuntimeError(f"No ready node matched worker {worker_id}")
    if lease["workflow_id"] != expected_workflow or lease["node_id"] != expected_node:
        raise RuntimeError(
            "The worker claimed older queued work. Use a fresh Fleet state directory for the demo."
        )
    return request_json(
        base_url,
        "POST",
        "/v1/completions",
        {
            "worker_id": worker_id,
            "lease_token": lease["lease_token"],
            "result": {
                "output": output,
                "artifacts": {artifact_name: artifact_content},
                "tokens_used": 128,
                "cost_usd": 0.0,
                "duration_seconds": 0.1,
                "quality": 1.0,
            },
        },
    )


def approval_allowed(auto_approve: bool) -> bool:
    if auto_approve:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input("Publish is awaiting human approval. Type APPROVE to continue: ")
    return answer.strip() == "APPROVE"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8833", help="Fleet base URL")
    parser.add_argument(
        "--approve-publish",
        action="store_true",
        help="Approve the fixture publish node non-interactively",
    )
    args = parser.parse_args()

    workflow = json.loads(Path(__file__).with_name("workflow.json").read_text(encoding="utf-8"))
    workflow_id = request_json(args.url, "POST", "/v1/workflows", workflow)["workflow_id"]
    print(f"submitted       {workflow_id}")

    register(args.url, "fixture-researcher", "market.research")
    source_result = claim_and_complete(
        args.url,
        "fixture-researcher",
        workflow_id,
        "source-map",
        {"sources_reviewed": 0, "live_research": False, "status": "fixture-only"},
        "source-map-fixture.md",
        (
            "# Source map fixture\n\n"
            "No live sources were queried. This artifact tests provenance flow.\n"
        ),
    )
    print(f"source-map      {source_result['artifacts'][0]['sha256'][:16]}...")

    register(args.url, "fixture-reviewer", "market.review")
    review_result = claim_and_complete(
        args.url,
        "fixture-reviewer",
        workflow_id,
        "evidence-review",
        {"unsupported_claims": 0, "decision": "fixture is correctly disclosed"},
        "review-fixture.md",
        (
            "# Evidence review fixture\n\n"
            "Result: no market claims are made by this deterministic demo.\n"
        ),
    )
    print(f"evidence-review {review_result['artifacts'][0]['sha256'][:16]}...")

    if not approval_allowed(args.approve_publish):
        print("publish          awaiting_approval")
        print(
            "Run again with --approve-publish on fresh state, or approve this node through "
            f"POST /v1/workflows/{workflow_id}/nodes/publish/approval."
        )
        return 0

    request_json(
        args.url,
        "POST",
        f"/v1/workflows/{workflow_id}/nodes/publish/approval",
        {"approved": True, "actor": "demo-operator"},
    )
    print("publish          approved by demo-operator")

    register(args.url, "fixture-publisher", "artifact.publish")
    publish_result = claim_and_complete(
        args.url,
        "fixture-publisher",
        workflow_id,
        "publish",
        {"published": True, "kind": "deterministic-contract-fixture"},
        "market-brief-fixture.md",
        "# Market brief fixture\n\nThis is an orchestration demo, not a market-research result.\n",
    )
    print(f"artifact         {publish_result['artifacts'][0]['sha256'][:16]}...")

    final = request_json(args.url, "GET", f"/v1/workflows/{workflow_id}")
    statuses = {node["node_id"]: node["status"] for node in final["nodes"]}
    print(json.dumps({"workflow_id": workflow_id, "status": final["status"], "nodes": statuses}))
    return 0 if final["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
