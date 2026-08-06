(() => {
  const active = window.location.hostname.endsWith("github.io")
    || new URLSearchParams(window.location.search).get("demo") === "1";
  if (!active) return;

  const task = "Review the checkout authorization change and report the highest-risk regression.";
  const answer = "High-risk regression found in `src/checkout/policy.py:42`: state mutation now occurs before authorization. Move the authorization guard ahead of the mutation and add a rollback assertion.";
  const startedAt = "2026-08-06T02:00:00Z";

  function traceEvent(runId, sequence, type, payload = {}, durationMs = null) {
    return {
      run_id: runId,
      sequence,
      type,
      at: new Date(Date.parse(startedAt) + sequence * 16).toISOString(),
      duration_ms: durationMs,
      payload,
    };
  }

  const baselineId = "run_baseline_demo";
  const candidateId = "run_regression_demo";
  const baselineEvents = [
    traceEvent(baselineId, 1, "run.started", { adapter: "demo", task, max_turns: 6 }),
    traceEvent(baselineId, 2, "model.requested", { turn: 1, message_count: 2, tool_count: 2 }),
    traceEvent(baselineId, 3, "model.completed", { turn: 1, finish_reason: "tool_calls", tool_calls: 1, text: "I will locate the policy path first." }, 18.2),
    traceEvent(baselineId, 4, "tool.requested", { turn: 1, call_id: "call_search_1", name: "search_repository", arguments: { query: "authorization before mutation" }, source: "local" }),
    traceEvent(baselineId, 5, "tool.approved", { call_id: "call_search_1", name: "search_repository", policy: "read-only-auto" }),
    traceEvent(baselineId, 6, "tool.completed", { call_id: "call_search_1", name: "search_repository", result: { matches: [{ path: "src/checkout/policy.py", line: 42 }] } }, 2.4),
    traceEvent(baselineId, 7, "model.requested", { turn: 2, message_count: 4, tool_count: 2 }),
    traceEvent(baselineId, 8, "model.completed", { turn: 2, finish_reason: "tool_calls", tool_calls: 1, text: "I will inspect the mutation order." }, 16.7),
    traceEvent(baselineId, 9, "tool.requested", { turn: 2, call_id: "call_inspect_1", name: "inspect_change", arguments: { path: "src/checkout/policy.py", focus: "authorization ordering" }, source: "local" }),
    traceEvent(baselineId, 10, "tool.approved", { call_id: "call_inspect_1", name: "inspect_change", policy: "read-only-auto" }),
    traceEvent(baselineId, 11, "tool.completed", { call_id: "call_inspect_1", name: "inspect_change", result: { risk: "high", signal: "mutation precedes authorization" } }, 1.8),
    traceEvent(baselineId, 12, "model.requested", { turn: 3, message_count: 6, tool_count: 2 }),
    traceEvent(baselineId, 13, "model.completed", { turn: 3, finish_reason: "stop", tool_calls: 0, text: answer }, 21.3),
    traceEvent(baselineId, 14, "run.completed", { turns: 3, answer }),
  ];
  const candidateEvents = [
    traceEvent(candidateId, 1, "run.started", { adapter: "regression-fixture", task, max_turns: 6 }),
    traceEvent(candidateId, 2, "model.requested", { turn: 1, message_count: 2, tool_count: 2 }),
    traceEvent(candidateId, 3, "model.completed", { turn: 1, finish_reason: "tool_calls", tool_calls: 1, text: "I will inspect the presumed path directly." }, 13.1),
    traceEvent(candidateId, 4, "tool.requested", { turn: 1, call_id: "call_inspect_regressed", name: "inspect_change", arguments: { path: "src/checkout/policy.py", focus: "authorization ordering" }, source: "local" }),
    traceEvent(candidateId, 5, "tool.approved", { call_id: "call_inspect_regressed", name: "inspect_change", policy: "read-only-auto" }),
    traceEvent(candidateId, 6, "tool.completed", { call_id: "call_inspect_regressed", name: "inspect_change", result: { risk: "high", signal: "mutation precedes authorization" } }, 1.6),
    traceEvent(candidateId, 7, "model.requested", { turn: 2, message_count: 4, tool_count: 2 }),
    traceEvent(candidateId, 8, "model.completed", { turn: 2, finish_reason: "stop", tool_calls: 0, text: answer }, 17.4),
    traceEvent(candidateId, 9, "run.completed", { turns: 2, answer }),
  ];

  function runRecord(id, adapter, events, offsetMs) {
    return {
      id,
      task,
      adapter,
      status: "completed",
      started_at: new Date(Date.parse(startedAt) + offsetMs).toISOString(),
      completed_at: new Date(Date.parse(startedAt) + offsetMs + 320).toISOString(),
      answer,
      error: null,
      events,
      metadata: { max_turns: 6, message_count: adapter === "demo" ? 7 : 5 },
    };
  }

  const demoRuns = [
    runRecord(candidateId, "regression-fixture", candidateEvents, 1000),
    runRecord(baselineId, "demo", baselineEvents, 0),
  ];
  const checks = [
    ["schema-fidelity", "Tool schema fidelity", "2 unique JSON Schema tool contracts discovered"],
    ["ordered-events", "Monotonic event stream", "14 events retained in strict per-run order"],
    ["tool-roundtrip", "Tool call round trip", "2 requested calls returned correlated results"],
    ["approval-boundary", "Read-only approval boundary", "Every tool crossed an explicit policy event"],
    ["terminal-state", "Single terminal state", "Run ended with one final event"],
    ["evidence-answer", "Evidence-bearing answer", "Final answer preserves file-level evidence"],
    ["policy-order", "Policy precedes execution", "Every approval precedes tool completion"],
    ["trace-contract", "Trace Contract invariants", "Projection has no violations"],
    ["artifact-roundtrip", "Artifact round trip", "Fingerprint survives serialization"],
    ["context-isolation", "Raw context isolation", "Serialized records exclude raw context"],
  ].map(([id, title, evidence]) => ({ id, title, status: "passed", evidence }));
  const conformance = { adapter: "demo", passed: 10, total: 10, checks, run_id: baselineId };
  const breakingComparison = {
    contract_version: "harnesslab.trace/v1",
    compatible: false,
    protocol_score: 78,
    baseline_run_id: baselineId,
    candidate_run_id: candidateId,
    baseline_fingerprint: "sha256:e5b1f33d4668710abd97c397eec9e696d0e5c506339e217afb0a66a4fbda773e",
    candidate_fingerprint: "sha256:7c57de0b026fa352cab648706ec8728e125828efcde4cbb358f695261c62a23c",
    content_match: false,
    differences: [
      { area: "event-sequence", severity: "breaking", detail: "Lifecycle event order changed", expected: baselineEvents.map((event) => event.type), actual: candidateEvents.map((event) => event.type) },
      { area: "tool-path", severity: "breaking", detail: "Tool request/completion path changed", expected: ["search_repository", "inspect_change"], actual: ["inspect_change"] },
      { area: "policy-path", severity: "breaking", detail: "Approval path changed", expected: ["search_repository", "inspect_change"], actual: ["inspect_change"] },
      { area: "content", severity: "notice", detail: "Redacted event payloads changed", expected: "sha256:3b9a4435...", actual: "sha256:bd317392..." },
    ],
  };

  function exactComparison(runId) {
    const fingerprint = runId === baselineId
      ? breakingComparison.baseline_fingerprint
      : breakingComparison.candidate_fingerprint;
    return {
      ...breakingComparison,
      compatible: true,
      protocol_score: 100,
      baseline_run_id: runId,
      candidate_run_id: runId,
      baseline_fingerprint: fingerprint,
      candidate_fingerprint: fingerprint,
      content_match: true,
      differences: [],
    };
  }

  window.HarnessLabDemo = {
    active: true,
    async request(url, options = {}) {
      const method = options.method || "GET";
      if (url === "/api/meta") {
        return {
          name: "HarnessLab",
          version: "0.2.0",
          mode: "static-demo",
          trace_contract: "harnesslab.trace/v1",
          protocol_status: "adapter-ready",
          adapters: ["demo", "regression-fixture"],
          tools: [
            { name: "search_repository", description: "Locate repository evidence.", input_schema: { required: ["query"] }, read_only: true, source: "local" },
            { name: "inspect_change", description: "Inspect deterministic risk signals.", input_schema: { required: ["path", "focus"] }, read_only: true, source: "local" },
          ],
          featured_run_id: baselineId,
          regression_run_id: candidateId,
        };
      }
      if (url === "/api/runs" && method === "GET") return demoRuns;
      if (url === "/api/conformance") return conformance;
      if (url === "/api/compare") {
        const body = JSON.parse(options.body || "{}");
        return body.baseline_run_id === body.candidate_run_id
          ? exactComparison(body.baseline_run_id)
          : breakingComparison;
      }
      if (url === "/api/runs" && method === "POST") {
        const body = JSON.parse(options.body || "{}");
        const created = structuredClone(demoRuns.find((run) => run.adapter === body.adapter) || demoRuns[1]);
        created.id = `run_static_${Date.now().toString(16)}`;
        created.task = body.task;
        created.started_at = new Date().toISOString();
        created.completed_at = new Date(Date.now() + 320).toISOString();
        created.events.forEach((event) => {
          event.run_id = created.id;
          if (event.type === "run.started") event.payload.task = body.task;
        });
        return created;
      }
      const runMatch = url.match(/^\/api\/runs\/([^/]+)$/);
      if (runMatch) return demoRuns.find((run) => run.id === decodeURIComponent(runMatch[1]));
      const artifactMatch = url.match(/^\/api\/runs\/([^/]+)\/artifact$/);
      if (artifactMatch) {
        const run = demoRuns.find((item) => item.id === decodeURIComponent(artifactMatch[1]));
        return {
          contract_version: "harnesslab.trace/v1",
          source_run_id: run.id,
          task: run.task,
          adapter: run.adapter,
          status: run.status,
          protocol_fingerprint: run.adapter === "demo" ? breakingComparison.baseline_fingerprint : breakingComparison.candidate_fingerprint,
          content_fingerprint: "sha256:static-demo-content",
          projection: { event_types: run.events.map((event) => event.type), model_path: [], tool_path: [], policy_path: [], terminal_event: "run.completed", terminal_status: "completed", violations: [] },
          events: run.events,
        };
      }
      throw new Error(`Static demo route not found: ${method} ${url}`);
    },
  };
})();
