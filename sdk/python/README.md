# EvoAgent Python SDK

The synchronous EvoAgent SDK provides typed access to the EvoAgent OS control
plane. It uses the same Pydantic contracts as the server and supports API-key,
tenant, and workspace scoping.

## Install

From the monorepo checkout:

```bash
pip install -e packages/contracts -e sdk/python
```

## Quick start

```python
from evoagent_contracts import AgentCreate, RunCreate
from evoagent_sdk import Client

with Client("http://localhost:8080", api_key="local-dev") as client:
    agent = client.create_agent(
        AgentCreate(
            workspace_id="ws_demo",
            name="researcher",
            role="Research analyst",
            model="gpt-5",
            system_prompt="Investigate the request and cite evidence.",
            capabilities=["research"],
        )
    )
    run = client.create_run(
        RunCreate(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            input={"question": "Summarize the latest evidence."},
        )
    )
```

`Client` automatically adds `/api/v1` to a server origin. Passing a URL that
already ends in `/api/v1` is also supported.

## Resources

The client exposes:

- `overview()`
- `workspaces()` and `create_workspace()`
- `agents()` and `create_agent()`
- `runs()`, `get_run()`, and `create_run()`
- `workflows()`, `get_workflow()`, and `create_workflow()`
- `approvals()` and `decide_approval()`
- `events()`, `skills()`, and `artifacts()`
- `demo()` for the end-to-end demonstration launch

The `list_*` aliases are available for codebases that prefer explicit list
method names. API failures raise `ApiError`; malformed successful responses
raise `ProtocolError`.

## Test

```bash
pytest sdk/python/tests
```
