from __future__ import annotations

import argparse
import os
import socket
import time
from typing import Any

import httpx


class Worker:
    def __init__(
        self,
        base_url: str,
        worker_id: str,
        capabilities: list[str],
        token: str = "",
        poll_seconds: float = 1.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=30,
        )
        self.worker_id = worker_id
        self.capabilities = capabilities
        self.poll_seconds = poll_seconds

    def register(self) -> None:
        response = self.client.post(
            "/api/v1/workers",
            json={
                "worker_id": self.worker_id,
                "capabilities": self.capabilities,
                "pool": "reference-remote",
                "max_concurrency": 1,
                "metadata": {"executor": "deterministic", "arbitrary_code": False},
            },
        )
        response.raise_for_status()

    def run(self, once: bool = False) -> None:
        self.register()
        while True:
            response = self.client.post(f"/api/v1/workers/{self.worker_id}/claims")
            response.raise_for_status()
            lease = response.json()["lease"]
            if lease is None:
                if once:
                    return
                time.sleep(self.poll_seconds)
                continue
            self.complete(lease)
            if once:
                return

    def complete(self, lease: dict[str, Any]) -> None:
        objective = str(lease["objective"])
        output = {
            "objective": objective,
            "status": "completed",
            "executor": "reference-deterministic",
        }
        artifact = (
            "# Worker result\n\n"
            f"Objective: {objective}\n\n"
            "This bounded reference worker does not execute arbitrary code.\n"
        )
        response = self.client.post(
            f"/api/v1/leases/{lease['lease_token']}/completion",
            json={
                "worker_id": self.worker_id,
                "output": output,
                "artifacts": {"worker-result.md": artifact},
                "tokens_used": min(128, lease["budget"]["tokens"]),
                "cost_usd": 0,
                "duration_seconds": 0.01,
                "quality": 1,
            },
        )
        response.raise_for_status()

    def close(self) -> None:
        self.client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bounded EvoAgent OS reference worker")
    parser.add_argument("--url", default=os.getenv("EVOAGENT_OS_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.getenv("EVOAGENT_OS_TOKEN", ""))
    parser.add_argument("--worker-id", default=f"worker-{socket.gethostname().lower()}")
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    capabilities = args.capability or ["research", "analysis", "writing", "artifacts"]
    worker = Worker(args.url, args.worker_id, capabilities, args.token, args.poll_seconds)
    try:
        worker.run(args.once)
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()


if __name__ == "__main__":
    main()
