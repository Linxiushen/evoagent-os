# EvoAgent Forge

[![CI](https://github.com/Linxiushen/evoagent-forge/actions/workflows/ci.yml/badge.svg)](https://github.com/Linxiushen/evoagent-forge/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Linxiushen/evoagent-forge)](https://github.com/Linxiushen/evoagent-forge/releases) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**A trusted software supply chain for agent skills.**

[中文文档](README.zh-CN.md) | [Architecture](docs/architecture.md) | [Security](docs/security-model.md) | [Paper](paper/PAPER.md)

Agent skills are executable supply-chain artifacts, not prompt snippets. EvoAgent Forge gives them a lifecycle: strict `SKILL.md` metadata, explicit capabilities, static scanning, deterministic `.evoskill` packaging, Ed25519 signatures, content-addressed immutable releases, registry search, executable cases and evaluation-gated evolution proposals.

## Workflow

```text
author -> validate -> scan -> evaluate -> deterministic package -> sign -> publish
                                                               -> verify -> install
feedback -> regression cases -> candidate copy -> same gates -> new version
```

## Quick start

```bash
pip install -e ".[dev]"
evoagent-forge init examples/weather --name weather-brief
evoagent-forge validate examples/weather
evoagent-forge scan examples/weather
evoagent-forge evaluate examples/weather
evoagent-forge package examples/weather
evoagent-forge keygen --private forge-private.pem --public forge-public.pem
evoagent-forge sign dist/weather-brief-0.1.0.evoskill --key forge-private.pem
evoagent-forge publish examples/weather dist/weather-brief-0.1.0.evoskill \
  --signature dist/weather-brief-0.1.0.evoskill.sig.json
evoagent-forge serve --port 8822
```

Open `http://127.0.0.1:8822` for the registry console.

## Skill contract

```yaml
---
name: incident-triage
version: 1.0.0
description: Collect evidence and classify an operational incident
license: Apache-2.0
entrypoint: main.py
capabilities:
  - name: network.http
    reason: Query the allowlisted observability API
compatibility:
  evoagent-runtime: ">=0.1"
---
```

The scanner detects embedded credentials, dynamic evaluation, shell execution, oversized files and capabilities inferred from imports but absent from the manifest. High and critical findings block packaging.

## Trust semantics

A valid signature proves which key signed exact artifact bytes. It does not prove the author is trustworthy or the code is safe. Consumers must pin an expected key fingerprint, inspect declared capabilities and run the artifact in an appropriate sandbox.

## Development

```bash
ruff check . && ruff format --check . && pytest -q
```

Apache-2.0.
