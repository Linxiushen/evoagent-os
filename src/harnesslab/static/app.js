const state = {
  meta: null,
  runs: [],
  selected: null,
  conformance: null,
  comparison: null,
  filter: "all",
};

const viewLabels = {
  trace: ["RUNS / LIVE TRACE", "Execution trace"],
  regression: ["QUALITY / REGRESSION", "Trace contract diff"],
  conformance: ["QUALITY / PROTOCOL", "Conformance matrix"],
  adapters: ["RUNTIME / PROVIDERS", "Adapter registry"],
  tools: ["RUNTIME / CAPABILITIES", "Tool registry"],
};

const eventMeta = {
  "run.started": ["Run started", "run", "circle-play"],
  "run.completed": ["Run completed", "run", "check"],
  "run.failed": ["Run failed", "run", "x"],
  "model.requested": ["Model request", "model", "send"],
  "model.completed": ["Model response", "model", "sparkles"],
  "tool.requested": ["Tool request", "tool", "wrench"],
  "tool.approved": ["Policy approval", "tool", "shield-check"],
  "tool.denied": ["Policy denial", "tool", "shield-x"],
  "tool.completed": ["Tool result", "tool", "package-check"],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function icons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
}

function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function duration(run) {
  if (!run?.completed_at) return "live";
  const ms = Math.max(0, new Date(run.completed_at) - new Date(run.started_at));
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

function compactPayload(event) {
  const payload = event.payload || {};
  if (event.type === "model.completed") return payload.text || `${payload.tool_calls || 0} tool calls`;
  if (event.type === "tool.requested") return `${payload.name}(${JSON.stringify(payload.arguments || {})})`;
  if (event.type === "tool.completed") return JSON.stringify(payload.result || {});
  if (event.type === "tool.approved") return `${payload.policy} · ${payload.name}`;
  if (event.type === "run.started") return payload.task;
  if (event.type === "run.completed") return payload.answer;
  if (event.type === "run.failed") return payload.error;
  if (event.type === "model.requested") return `${payload.message_count} messages · ${payload.tool_count} tools`;
  return JSON.stringify(payload);
}

async function request(url, options) {
  if (window.HarnessLabDemo?.active) return window.HarnessLabDemo.request(url, options);
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function loadAll() {
  const [meta, runs, conformance] = await Promise.all([
    request("/api/meta"),
    request("/api/runs"),
    request("/api/conformance"),
  ]);
  state.meta = meta;
  state.runs = runs;
  state.conformance = conformance;
  const selectedId = state.selected?.id || meta.featured_run_id || runs[0]?.id;
  state.selected = runs.find((run) => run.id === selectedId) || runs[0] || null;
  render();
}

function render() {
  renderMeta();
  renderRuns();
  renderSelected();
  renderComparison();
  renderConformance();
  renderAdapters();
  renderTools();
  icons();
}

function renderMeta() {
  if (!state.meta) return;
  $("#version").textContent = `v${state.meta.version}`;
  $(".live-state span:last-child").textContent = state.meta.mode === "static-demo"
    ? "Static demo"
    : "Runtime online";
  const select = $("#adapter-select");
  const current = select.value;
  select.innerHTML = state.meta.adapters.map((name) => `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`).join("");
  if (state.meta.adapters.includes(current)) select.value = current;
  $("#metric-checks").textContent = state.conformance ? `${state.conformance.passed}/${state.conformance.total}` : "pending";
}

function renderRuns() {
  $("#run-count").textContent = state.runs.length;
  $("#run-list").innerHTML = state.runs.map((run) => `
    <button class="run-item ${state.selected?.id === run.id ? "active" : ""}" data-run-id="${escapeHTML(run.id)}">
      <span class="run-item-top"><code>${escapeHTML(run.id.replace("run_", "#"))}</code><span class="run-dot ${escapeHTML(run.status)}"></span></span>
      <p>${escapeHTML(run.task)}</p>
      <small>${escapeHTML(run.adapter)} · ${formatTime(run.started_at)}</small>
    </button>
  `).join("") || '<div class="empty-state">No runs</div>';
  $$(".run-item").forEach((button) => button.addEventListener("click", () => selectRun(button.dataset.runId)));
}

function renderSelected() {
  const run = state.selected;
  if (!run) return;
  $("#selected-run-id").textContent = run.id;
  $("#selected-task").textContent = run.task;
  $("#selected-status").textContent = run.status;
  $("#selected-status").className = `status ${run.status}`;
  $("#metric-adapter").textContent = run.adapter;
  const events = (run.events || []).filter((event) => state.filter === "all" || event.type.startsWith(`${state.filter}.`));
  $("#event-count").textContent = `${run.events?.length || 0} events`;
  $("#run-duration").textContent = duration(run);
  $("#timeline").innerHTML = events.map(renderEvent).join("") || '<div class="empty-state">Awaiting events</div>';
  $("#run-facts").innerHTML = `
    <dt>Adapter</dt><dd>${escapeHTML(run.adapter)}</dd>
    <dt>Turns</dt><dd>${escapeHTML(run.metadata?.max_turns || "-")} max</dd>
    <dt>Started</dt><dd>${formatTime(run.started_at)}</dd>
    <dt>Duration</dt><dd>${duration(run)}</dd>
    <dt>Messages</dt><dd>${run.metadata?.message_count || 0}</dd>
    <dt>Events</dt><dd>${run.events?.length || 0}</dd>
  `;
  $("#run-answer").textContent = run.answer || run.error || "No completed output.";
  icons();
}

function renderEvent(event) {
  const [label, kind, icon] = eventMeta[event.type] || [event.type, "run", "circle"];
  const detail = compactPayload(event);
  const elapsed = event.duration_ms == null ? `#${event.sequence}` : `${Number(event.duration_ms).toFixed(1)} ms`;
  return `
    <article class="event-row">
      <span class="event-node ${kind}"><i data-lucide="${icon}"></i></span>
      <div class="event-body">
        <div class="event-title"><strong>${escapeHTML(label)}</strong><code>${escapeHTML(event.type)}</code></div>
        <p class="event-detail">${escapeHTML(detail)}</p>
      </div>
      <time class="event-time" datetime="${escapeHTML(event.at)}">${escapeHTML(elapsed)}</time>
    </article>`;
}

function renderConformance() {
  const report = state.conformance;
  if (!report) return;
  $("#score-value").textContent = `${report.passed}/${report.total}`;
  $("#matrix-visual").innerHTML = report.checks.map((check, index) => `
    <div class="matrix-cell ${escapeHTML(check.status)}"><span>${String(index + 1).padStart(2, "0")}</span><strong>${escapeHTML(check.title)}</strong></div>
  `).join("");
  $("#check-table").innerHTML = report.checks.map((check) => `
    <tr><td><strong>${escapeHTML(check.title)}</strong><br><code>${escapeHTML(check.id)}</code></td><td><span class="check-badge ${check.status === "failed" ? "failed" : ""}">${escapeHTML(check.status)}</span></td><td>${escapeHTML(check.evidence)}</td></tr>
  `).join("");
}

function renderComparison() {
  const baseline = $("#baseline-run");
  const candidate = $("#candidate-run");
  const options = state.runs.map((run) => (
    `<option value="${escapeHTML(run.id)}">${escapeHTML(run.id.replace("run_", "#"))} / ${escapeHTML(run.adapter)}</option>`
  )).join("");
  const previousBaseline = baseline.value;
  const previousCandidate = candidate.value;
  baseline.innerHTML = options;
  candidate.innerHTML = options;
  baseline.value = state.runs.some((run) => run.id === previousBaseline)
    ? previousBaseline
    : (state.runs[1]?.id || state.runs[0]?.id || "");
  candidate.value = state.runs.some((run) => run.id === previousCandidate)
    ? previousCandidate
    : (state.runs[0]?.id || "");
  $("#compare-runs").disabled = state.runs.length === 0;

  const result = state.comparison;
  if (!result) {
    $("#diff-table").innerHTML = '<tr><td colspan="4" class="table-empty">Run a comparison to inspect structural drift.</td></tr>';
    return;
  }
  $("#compare-score").textContent = `${result.protocol_score}%`;
  $("#compare-verdict").textContent = result.compatible ? "Compatible" : "Breaking change";
  $("#compare-verdict").className = result.compatible ? "verdict-ok" : "verdict-bad";
  $("#contract-version").textContent = result.contract_version;
  $("#content-verdict").textContent = result.content_match ? "Exact match" : "Payload changed";
  $("#baseline-fingerprint").textContent = result.baseline_fingerprint;
  $("#candidate-fingerprint").textContent = result.candidate_fingerprint;
  $("#diff-table").innerHTML = result.differences.length
    ? result.differences.map((difference) => `
      <tr>
        <td><strong>${escapeHTML(difference.area)}</strong></td>
        <td><span class="check-badge ${difference.severity === "breaking" ? "failed" : "notice"}">${escapeHTML(difference.severity)}</span></td>
        <td>${escapeHTML(difference.detail)}</td>
        <td><code>${escapeHTML(compactValue(difference.expected))}</code><span class="diff-arrow">-&gt;</span><code>${escapeHTML(compactValue(difference.actual))}</code></td>
      </tr>
    `).join("")
    : '<tr><td colspan="4" class="table-empty success-empty">No structural or content drift detected.</td></tr>';
}

function compactValue(value) {
  const encoded = typeof value === "string" ? value : JSON.stringify(value);
  return encoded.length > 180 ? `${encoded.slice(0, 177)}...` : encoded;
}

function renderAdapters() {
  if (!state.meta) return;
  const copy = {
    demo: ["Deterministic fixture", "Offline reference adapter for repeatable traces and CI."],
    "regression-fixture": ["Regression fixture", "Deliberately skips a tool step to prove structural drift detection."],
    "deepseek-api": ["DeepSeek API", "Public chat-completions adapter enabled by DEEPSEEK_API_KEY."],
    "openai-compatible": ["Compatible endpoint", "Environment-configured adapter for compatible model APIs."],
  };
  const adapters = [...state.meta.adapters, "deepseek-harness (adapter boundary)"];
  $("#adapter-grid").innerHTML = adapters.map((name) => {
    const isBoundary = name.includes("boundary");
    const details = isBoundary ? ["Protocol boundary", "Isolated capability probe ready for the preview specification."] : (copy[name] || [name, "Registered runtime adapter."]);
    return `<article class="adapter-card">
      <div class="adapter-card-head"><span class="adapter-icon"><i data-lucide="${isBoundary ? "scan-search" : "waypoints"}"></i></span><span class="check-badge ${isBoundary ? "" : ""}">${isBoundary ? "ready" : "active"}</span></div>
      <h2>${escapeHTML(details[0])}</h2><p>${escapeHTML(details[1])}</p>
      <footer><span>${escapeHTML(name)}</span><span>${isBoundary ? "discovery" : "complete()"}</span></footer>
    </article>`;
  }).join("");
}

function renderTools() {
  if (!state.meta) return;
  $("#tool-table").innerHTML = state.meta.tools.map((tool) => `
    <tr><td><strong>${escapeHTML(tool.name)}</strong><br><small>${escapeHTML(tool.description)}</small></td><td><code>${escapeHTML(tool.source)}</code></td><td><span class="check-badge">${tool.read_only ? "read only" : "approval"}</span></td><td><code>${escapeHTML((tool.input_schema.required || []).join(", ") || "none")}</code></td></tr>
  `).join("");
}

async function selectRun(runId) {
  state.selected = await request(`/api/runs/${encodeURIComponent(runId)}`);
  renderRuns();
  renderSelected();
  if (["queued", "running"].includes(state.selected.status)) watchRun(runId);
}

function watchRun(runId) {
  const stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  const refresh = async () => {
    state.selected = await request(`/api/runs/${encodeURIComponent(runId)}`);
    state.runs = await request("/api/runs");
    renderRuns();
    renderSelected();
    if (["completed", "failed", "cancelled"].includes(state.selected.status)) stream.close();
  };
  Object.keys(eventMeta).forEach((name) => stream.addEventListener(name, refresh));
  stream.onerror = () => stream.close();
}

function switchView(name) {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${name}-view`));
  $("#view-eyebrow").textContent = viewLabels[name][0];
  $("#view-title").textContent = viewLabels[name][1];
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2200);
}

$("#run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const run = await request("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: $("#task-input").value, adapter: $("#adapter-select").value }),
    });
    state.runs.unshift(run);
    state.selected = run;
    renderRuns();
    renderSelected();
    if (["queued", "running"].includes(run.status)) watchRun(run.id);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#run-checks").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    state.conformance = await request("/api/conformance", { method: "POST" });
    renderConformance();
    renderMeta();
    toast(`${state.conformance.passed}/${state.conformance.total} checks passed`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#compare-runs").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    state.comparison = await request("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        baseline_run_id: $("#baseline-run").value,
        candidate_run_id: $("#candidate-run").value,
      }),
    });
    renderComparison();
    toast(state.comparison.compatible ? "Trace contract compatible" : "Breaking trace drift found");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#export-trace").addEventListener("click", async () => {
  if (!state.selected) return;
  try {
    const artifact = await request(`/api/runs/${encodeURIComponent(state.selected.id)}/artifact`);
    const blob = new Blob([JSON.stringify(artifact, null, 2)], { type: "application/json" });
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = `${state.selected.id}.trace.json`;
    link.click();
    URL.revokeObjectURL(href);
    toast("Trace Contract exported");
  } catch (error) {
    toast(error.message);
  }
});

$("#copy-id").addEventListener("click", async () => {
  if (!state.selected) return;
  await navigator.clipboard.writeText(state.selected.id);
  toast("Run ID copied");
});

$("#refresh").addEventListener("click", () => loadAll().catch((error) => toast(error.message)));
$$('.nav-item').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$$('.segmented button').forEach((button) => button.addEventListener("click", () => {
  state.filter = button.dataset.filter;
  $$('.segmented button').forEach((item) => item.classList.toggle("active", item === button));
  renderSelected();
}));

loadAll().catch((error) => toast(error.message));
