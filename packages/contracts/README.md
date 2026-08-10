# EvoAgent Contracts

`evoagent-contracts` is the versioned data contract shared by the EvoAgent OS
control plane, services, and client SDKs. It contains no network or storage
code and is safe to use at API boundaries.

## Install

```bash
pip install -e packages/contracts
```

The package requires Python 3.11+ and Pydantic v2.

## Use

```python
from evoagent_contracts import AgentCreate, RunCreate, UnifiedEvent

agent = AgentCreate(
    workspace_id="ws_demo",
    name="researcher",
    role="Research analyst",
    model="gpt-5",
    system_prompt="Investigate the request and cite evidence.",
)

run = RunCreate(
    workspace_id=agent.workspace_id,
    agent_id="agt_researcher",
    input={"question": "What changed?"},
)
```

Write models reject unknown fields so client mistakes fail early. Summary
models retain unknown server fields, allowing older clients to read additive
API changes.

## Export JSON Schema

Write a single schema bundle containing every public model:

```bash
evoagent-contracts-schema build/evoagent-contracts.schema.json
```

The equivalent module command is:

```bash
python -m evoagent_contracts.schema -
```

Using `-` writes the schema to stdout. Output is deterministic and can be used
to generate TypeScript, OpenAPI components, or conformance fixtures.

## Test

```bash
pytest packages/contracts/tests
```

