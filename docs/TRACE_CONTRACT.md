# HarnessLab Trace Contract v1

`harnesslab.trace/v1` is a portable artifact for detecting structural regressions in an agent
harness. It describes observable lifecycle behavior, not a provider's private wire format.

## Artifact

```json
{
  "contract_version": "harnesslab.trace/v1",
  "source_run_id": "run_123",
  "task": "Review the authorization change",
  "adapter": "demo",
  "status": "completed",
  "protocol_fingerprint": "sha256:...",
  "content_fingerprint": "sha256:...",
  "projection": {
    "event_types": ["run.started", "model.requested", "run.completed"],
    "model_path": ["model.requested:2", "model.completed:stop"],
    "tool_path": [],
    "policy_path": [],
    "terminal_event": "run.completed",
    "terminal_status": "completed",
    "violations": []
  },
  "events": []
}
```

The full artifact includes redacted events. Timestamps and durations remain useful evidence but do
not participate in either fingerprint.

## Protocol fingerprint

The protocol fingerprint hashes the canonical trace projection:

- Ordered lifecycle event types.
- Model request and finish-reason path.
- Tool request and completion path.
- Approval or denial path.
- Terminal event and run status.
- Detected invariant violations.

Run IDs, timestamps, durations, usage counters, and free-form model text are excluded. Two runs can
therefore share a protocol fingerprint even when latency or wording changes.

## Content fingerprint

The content fingerprint hashes stable event payloads after removing volatile usage, model text,
and final-answer fields. It is stricter than the protocol fingerprint and is useful for
deterministic fixtures.

`harnesslab verify` reports content drift as a notice by default. Pass `--strict-content` to make
that notice fail CI.

## Compatibility rules

A candidate is structurally compatible when all of these remain true:

1. Lifecycle event order matches the baseline.
2. Tool request and completion paths match.
3. Approval and denial paths match.
4. The terminal event matches.
5. The candidate has no Trace Contract invariant violations.

The comparison also reports an event-sequence similarity score from 0 to 100. The boolean
compatibility result, not the score alone, controls the default CLI exit code.

## Invariants

Trace Contract v1 detects:

- Non-monotonic event sequence numbers.
- Missing or duplicate terminal events.
- A terminal event that is not last.
- Tool requests without an approval or denial decision.
- Policy events that occur before their tool request.
- Tool completions that occur before their policy decision.
- Approved tools without a completion event.
- Denied tools that nevertheless emit a completion event.
- Duplicate or orphaned request, policy, and completion events.

## Redaction

HarnessLab recursively redacts common credential keys, including `api_key`, `authorization`,
`password`, `secret`, and token variants. Inline bearer credentials and common key prefixes are
also removed.

Redaction happens before event publication, so SSE consumers, the workbench, and exported
artifacts see the same protected payload. Raw model context is used only inside the active runtime
and is excluded from serialized run records.

Redaction is defense in depth, not a substitute for avoiding secrets in prompts and tool outputs.

## CI example

Record a reviewed baseline:

```bash
harnesslab snapshot "Review the checkout authorization change" -o tests/baselines/checkout.trace.json
```

Verify it in CI:

```bash
harnesslab verify tests/baselines/checkout.trace.json
```

Compare two saved artifacts without calling a model:

```bash
harnesslab compare before.trace.json after.trace.json --strict-content
```
