# EvoAgent Runtime

[![CI](https://github.com/Linxiushen/evoagent-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/Linxiushen/evoagent-runtime/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Linxiushen/evoagent-runtime)](https://github.com/Linxiushen/evoagent-runtime/releases) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**A local-first agent gateway that can improve without silently rewriting itself.**

[中文文档](README.zh-CN.md) | [Architecture](docs/architecture.md) | [Threat model](docs/threat-model.md) | [Paper](paper/PAPER.md)

EvoAgent Runtime is an installable control plane for long-lived AI agents. It combines persistent sessions, a typed HTTP/WebSocket gateway, tool policy, human approval, scheduling, memory, an immutable event ledger, and evaluation-gated prompt evolution in one small operational system.

It is designed for engineers who need more than a chat loop: a process that stays online, accepts work from channels, resumes after approval, exposes evidence, and only promotes a behavioral change after an independent regression gate.

## Why this exists

Most agent demos optimize for the first successful run. Production agents fail later: a tool call is replayed, a prompt changes without provenance, an HTTP tool reaches an internal address, or an operator cannot reconstruct why a side effect occurred. EvoAgent Runtime treats those as control-plane problems.

## Capabilities

- FastAPI Gateway with typed HTTP and WebSocket RPC
- SQLite WAL state for sessions, runs, messages, approvals, jobs, prompts and events
- deterministic offline provider plus OpenAI-compatible model endpoint
- FTS5 long-term memory with relevant-memory injection
- typed tools with low/medium/high risk classification
- mandatory operator approval and resumable high-risk tool calls
- workspace path containment and HTTPS allowlist/SSRF defenses
- interval scheduling for 24/7 workers
- feedback-derived prompt candidates, scenario evaluation and explicit promotion
- responsive operations console, not a marketing shell

## Five-minute run

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
evoagent-runtime doctor
evoagent-runtime demo --state-dir .demo
evoagent-runtime serve --state-dir .demo --port 8811
```

Open `http://127.0.0.1:8811`. The offline provider makes onboarding and CI deterministic. For a model endpoint:

```bash
export EVOAGENT_PROVIDER=openai
export EVOAGENT_API_KEY=...
export EVOAGENT_MODEL=gpt-4o-mini
export EVOAGENT_GATEWAY_TOKEN=replace-me
evoagent-runtime serve
```

## Gateway example

```bash
curl -X POST http://127.0.0.1:8811/v1/messages/wait \
  -H "Authorization: Bearer $EVOAGENT_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"api","peer_id":"alice","text":"remember: freeze is Friday"}'
```

Risky tools pause at `awaiting_approval`. Resume them through the console or `POST /v1/approvals/{id}`. Every transition is recorded in `/v1/events`.

## Governed self-evolution

```text
feedback -> candidate prompt -> baseline/candidate scenarios -> safety gate -> human promotion
```

The live prompt is never mutated in place. A candidate retains its parent version and scores; only a passed candidate can be promoted. This is deliberately conservative: evolution is a deployment, not a side effect of conversation.

## Development

```bash
ruff check .
ruff format --check .
pytest -q
docker compose up --build
```

Apache-2.0. Contributions and security reports are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
