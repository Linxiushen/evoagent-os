(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const PAGE_META = {
    overview: {
      eyebrow: "Fleet operations",
      title: "Overview",
      description: "Live execution, quality, spend, and governance across your agent fleet."
    },
    runs: {
      eyebrow: "Execution ledger",
      title: "Runs",
      description: "Inspect every agent run, its cost, latency, artifacts, and execution state."
    },
    workflows: {
      eyebrow: "Durable orchestration",
      title: "Workflows",
      description: "Trace node dependencies, budget consumption, workers, and approval gates."
    },
    agents: {
      eyebrow: "Fleet registry",
      title: "Agents",
      description: "Monitor specialized agents, model assignments, capacity, and quality."
    },
    skills: {
      eyebrow: "Supply chain",
      title: "Skills",
      description: "Review signed capabilities, versions, trust status, and evaluation results."
    },
    approvals: {
      eyebrow: "Human oversight",
      title: "Approvals",
      description: "Resolve policy gates with complete requester and workflow context."
    },
    evaluations: {
      eyebrow: "Quality intelligence",
      title: "Evaluations",
      description: "Compare routes, regression signals, conformance, and release readiness."
    }
  };

  const ENDPOINTS = ["overview", "workspaces", "agents", "runs", "workflows", "approvals", "skills", "events", "evaluations"];
  const state = {
    view: "overview",
    workspaceId: "",
    query: "",
    runFilter: "all",
    approvalFilter: "pending",
    loading: true,
    refreshing: false,
    online: false,
    lastUpdatedAt: null,
    controller: null,
    selectedWorkflowId: "",
    workflowDetails: new Map(),
    approvalPending: new Set(),
    errors: {},
    overview: {},
    workspaces: [],
    agents: [],
    runs: [],
    workflows: [],
    approvals: [],
    skills: [],
    events: [],
    evaluations: []
  };

  const elements = {};

  function $(selector, root = document) {
    return root.querySelector(selector);
  }

  function $$(selector, root = document) {
    return Array.from(root.querySelectorAll(selector));
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[character]);
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }

  function safeClass(value) {
    return String(value ?? "unknown").toLowerCase().trim().replace(/[^a-z0-9_-]+/g, "_");
  }

  function first(object, keys, fallback = undefined) {
    for (const key of keys) {
      if (object && object[key] !== undefined && object[key] !== null && object[key] !== "") return object[key];
    }
    return fallback;
  }

  function arrayValue(value) {
    if (Array.isArray(value)) return value;
    if (value && Array.isArray(value.items)) return value.items;
    if (value && Array.isArray(value.data)) return value.data;
    return [];
  }

  function numberValue(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min = 0, max = 100) {
    return Math.min(max, Math.max(min, numberValue(value)));
  }

  function normalizePercent(value) {
    const numeric = numberValue(value);
    return clamp(Math.abs(numeric) <= 1 ? numeric * 100 : numeric);
  }

  function itemId(item, type = "item") {
    const keyMap = {
      run: ["run_id", "id"],
      workflow: ["workflow_id", "id"],
      agent: ["agent_id", "id"],
      approval: ["approval_id", "id"],
      skill: ["skill_id", "id", "name"],
      workspace: ["workspace_id", "id"]
    };
    return String(first(item, keyMap[type] || ["id"], ""));
  }

  function itemStatus(item, fallback = "unknown") {
    return String(first(item, ["status", "state", "health"], fallback)).toLowerCase().replace(/\s+/g, "_");
  }

  function shortId(value, length = 10) {
    const text = String(value ?? "");
    if (!text) return "-";
    return text.length > length + 4 ? `${text.slice(0, length)}...` : text;
  }

  function formatNumber(value, maximumFractionDigits = 1) {
    const numeric = numberValue(value);
    if (Math.abs(numeric) >= 1000) {
      return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits }).format(numeric);
    }
    return new Intl.NumberFormat("en", { maximumFractionDigits }).format(numeric);
  }

  function formatUsd(value) {
    const numeric = numberValue(value);
    if (numeric < .01 && numeric > 0) return `$${numeric.toFixed(4)}`;
    return new Intl.NumberFormat("en", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(numeric);
  }

  function formatDuration(value) {
    const milliseconds = numberValue(value);
    if (!milliseconds) return "-";
    if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
    if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0)} s`;
    return `${Math.floor(milliseconds / 60000)}m ${Math.round((milliseconds % 60000) / 1000)}s`;
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatDate(value) {
    const date = parseDate(value);
    if (!date) return "-";
    return new Intl.DateTimeFormat("en", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    }).format(date);
  }

  function formatRelative(value) {
    const date = parseDate(value);
    if (!date) return "-";
    const delta = date.getTime() - Date.now();
    const absolute = Math.abs(delta);
    const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
    if (absolute < 60000) return formatter.format(Math.round(delta / 1000), "second");
    if (absolute < 3600000) return formatter.format(Math.round(delta / 60000), "minute");
    if (absolute < 86400000) return formatter.format(Math.round(delta / 3600000), "hour");
    return formatter.format(Math.round(delta / 86400000), "day");
  }

  function statusBadge(status, label = "") {
    const normalized = safeClass(status);
    const text = label || String(status || "unknown").replace(/_/g, " ");
    return `<span class="status-badge ${normalized}">${escapeHtml(text)}</span>`;
  }

  function riskBadge(risk) {
    const normalized = safeClass(risk || "medium");
    return `<span class="risk-badge ${normalized}">${escapeHtml(risk || "medium")} risk</span>`;
  }

  function icon(name) {
    return `<i data-lucide="${escapeAttribute(name)}" aria-hidden="true"></i>`;
  }

  function hydrateIcons() {
    requestAnimationFrame(() => {
      if (window.lucide && typeof window.lucide.createIcons === "function") {
        window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
        document.documentElement.classList.add("icons-ready");
      }
    });
  }

  function matchesQuery(item) {
    if (!state.query) return true;
    const needle = state.query.toLowerCase();
    try {
      return JSON.stringify(item).toLowerCase().includes(needle);
    } catch (_error) {
      return String(item).toLowerCase().includes(needle);
    }
  }

  function getWorkspaceQuery(path) {
    if (!state.workspaceId || path === "/workspaces") return "";
    return `workspace_id=${encodeURIComponent(state.workspaceId)}`;
  }

  async function apiRequest(path, options = {}) {
    const workspaceQuery = getWorkspaceQuery(path);
    const separator = path.includes("?") ? "&" : "?";
    const url = `${API_ROOT}${path}${workspaceQuery ? separator + workspaceQuery : ""}`;
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { ...options, headers });
    const contentType = response.headers.get("content-type") || "";
    let payload;
    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      const text = await response.text();
      payload = text ? { detail: text } : {};
    }
    if (!response.ok) {
      const detail = first(payload, ["detail", "message", "error"], `Request failed with status ${response.status}`);
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function loadingMarkup() {
    return `
      <div class="loading-state" role="status">
        <div class="state-content" style="max-width:none;width:100%">
          <div class="skeleton-metrics" aria-hidden="true">
            <span></span><span></span><span></span><span></span><span></span>
          </div>
          <div class="skeleton-stack" aria-hidden="true">
            <div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div>
          </div>
          <span class="sr-only">Loading control plane data</span>
        </div>
      </div>`;
  }

  function emptyMarkup(title, description, iconName = "inbox", compact = false) {
    return `
      <div class="empty-state${compact ? " compact-state" : ""}">
        <div class="state-content">
          <span class="state-icon">${icon(iconName)}</span>
          <h3>${escapeHtml(title)}</h3>
          <p>${escapeHtml(description)}</p>
        </div>
      </div>`;
  }

  function errorMarkup(endpoint, compact = false) {
    const message = state.errors[endpoint] || `Could not load ${endpoint}.`;
    return `
      <div class="error-state${compact ? " compact-state" : ""}">
        <div class="state-content">
          <span class="state-icon">${icon("triangle-alert")}</span>
          <h3>Data unavailable</h3>
          <p>${escapeHtml(message)}</p>
          <button class="secondary-button" type="button" data-action="retry">${icon("refresh-cw")} Retry</button>
        </div>
      </div>`;
  }

  function showToast(title, message = "", type = "success", timeout = 4200) {
    const toast = document.createElement("div");
    const iconName = type === "error" ? "circle-alert" : type === "warning" ? "triangle-alert" : "circle-check";
    toast.className = `toast ${safeClass(type)}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    toast.innerHTML = `
      ${icon(iconName)}
      <div class="toast-copy"><strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}</div>
      <button class="toast-dismiss" type="button" aria-label="Dismiss notification" data-action="dismiss-toast">${icon("x")}</button>`;
    elements.toastRegion.appendChild(toast);
    hydrateIcons();
    const remove = () => toast.remove();
    toast._timer = window.setTimeout(remove, timeout);
  }

  function setApiStatus(online, message = "") {
    state.online = online;
    const apiDot = $(".health-dot", elements.apiStatus);
    const apiLabel = $("span:last-child", elements.apiStatus);
    const sidebarDot = elements.sidebarHealthDot;
    apiDot.classList.toggle("online", online);
    apiDot.classList.toggle("offline", !online);
    sidebarDot.classList.toggle("online", online);
    sidebarDot.classList.toggle("offline", !online);
    apiLabel.textContent = online ? "Live" : "Offline";
    elements.sidebarHealthLabel.textContent = online ? "All systems live" : "Connection issue";
    elements.sidebarHealthMeta.textContent = online
      ? `${numberValue(state.overview?.counts?.online_workers)} workers online`
      : "Control plane unavailable";
    elements.connectionBanner.hidden = online;
    if (!online) elements.connectionError.textContent = message || "Could not load current data.";
  }

  function currentWorkspaceName() {
    if (!state.workspaceId) return "All workspaces";
    const workspace = state.workspaces.find((item) => itemId(item, "workspace") === state.workspaceId);
    return first(workspace, ["name", "display_name", "slug"], "Selected workspace");
  }

  function renderWorkspaceSelector() {
    const previous = state.workspaceId;
    const options = [`<option value="">All workspaces</option>`];
    state.workspaces.forEach((workspace) => {
      const id = itemId(workspace, "workspace");
      const name = first(workspace, ["name", "display_name", "slug"], shortId(id));
      options.push(`<option value="${escapeAttribute(id)}"${id === previous ? " selected" : ""}>${escapeHtml(name)}</option>`);
    });
    elements.workspaceSelect.innerHTML = options.join("");
    elements.workspaceSelect.value = previous;
  }

  function updateNavCounts() {
    const counts = state.overview?.counts || {};
    elements.navRuns.textContent = String(first(counts, ["runs"], state.runs.length) || "");
    elements.navWorkflows.textContent = String(first(counts, ["active_workflows"], state.workflows.length) || "");
    elements.navAgents.textContent = String(first(counts, ["agents"], state.agents.length) || "");
    elements.navSkills.textContent = String(first(counts, ["skills"], state.skills.length) || "");
    const pending = state.approvals.filter((approval) => ["pending", "awaiting_approval", "requested", "open"].includes(itemStatus(approval, "pending"))).length;
    elements.navApprovals.textContent = String(first(counts, ["pending_approvals"], pending) || "");
  }

  async function loadAll({ notify = false } = {}) {
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    const signal = state.controller.signal;
    state.refreshing = !state.loading;
    elements.refreshButton.classList.add("loading");
    elements.refreshButton.disabled = true;
    if (state.loading) renderCurrentView();

    const settled = await Promise.allSettled(ENDPOINTS.map(async (endpoint) => {
      const payload = await apiRequest(`/${endpoint}`, { signal });
      return { endpoint, payload };
    }));

    if (signal.aborted) return;
    state.errors = {};
    let successful = 0;
    settled.forEach((result, index) => {
      const endpoint = ENDPOINTS[index];
      if (result.status === "fulfilled") {
        successful += 1;
        const payload = result.value.payload;
        state[endpoint] = endpoint === "overview" ? (payload || {}) : arrayValue(payload);
      } else if (result.reason?.name !== "AbortError") {
        state.errors[endpoint] = result.reason?.message || `Could not load ${endpoint}.`;
      }
    });

    state.loading = false;
    state.refreshing = false;
    state.lastUpdatedAt = new Date();
    elements.refreshButton.classList.remove("loading");
    elements.refreshButton.disabled = false;
    renderWorkspaceSelector();
    updateNavCounts();
    setApiStatus(successful > 0, Object.values(state.errors)[0]);

    if (!state.selectedWorkflowId && state.workflows.length) {
      state.selectedWorkflowId = itemId(state.workflows[0], "workflow");
    }
    renderCurrentView();
    if (notify) {
      if (successful === ENDPOINTS.length) showToast("Data refreshed", `Updated ${formatRelative(state.lastUpdatedAt)}`);
      else if (successful > 0) showToast("Partially refreshed", `${ENDPOINTS.length - successful} data source(s) unavailable`, "warning");
      else showToast("Refresh failed", Object.values(state.errors)[0] || "Control plane unavailable", "error");
    }

    if (state.selectedWorkflowId) loadWorkflowDetail(state.selectedWorkflowId, false);
  }

  function renderMetric(label, value, detail, iconName, trend = "") {
    return `
      <article class="metric-cell">
        <div class="metric-label"><span>${escapeHtml(label)}</span>${icon(iconName)}</div>
        <div class="metric-value"><strong>${escapeHtml(value)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</div>
        <div class="metric-trend ${trend ? "positive" : ""}">${trend ? icon("trending-up") : icon("minus")}<span>${escapeHtml(trend || "Current workspace")}</span></div>
      </article>`;
  }

  function overviewMetrics() {
    const counts = state.overview.counts || {};
    const metrics = state.overview.metrics || {};
    const activeWorkflows = first(counts, ["active_workflows"], state.workflows.filter((item) => ["running", "active", "in_progress"].includes(itemStatus(item))).length);
    const pendingApprovals = first(counts, ["pending_approvals"], state.approvals.filter((item) => ["pending", "requested", "open", "awaiting_approval"].includes(itemStatus(item, "pending"))).length);
    const onlineWorkers = first(counts, ["online_workers"], state.agents.filter((item) => ["online", "ready", "active", "healthy"].includes(itemStatus(item))).length);
    const cost = first(metrics, ["cost_usd", "cost", "spend_usd"], state.runs.reduce((sum, run) => sum + numberValue(first(run, ["cost_usd", "cost"])), 0));
    const successRate = normalizePercent(first(metrics, ["success_rate", "success"], successRateFromRuns()));
    return `
      <div class="metrics-strip">
        ${renderMetric("Active workflows", formatNumber(activeWorkflows, 0), "running", "workflow")}
        ${renderMetric("Pending approvals", formatNumber(pendingApprovals, 0), "need review", "shield-alert")}
        ${renderMetric("Online workers", formatNumber(onlineWorkers, 0), "available", "radio-tower")}
        ${renderMetric("Current spend", formatUsd(cost), "USD", "circle-dollar-sign")}
        ${renderMetric("Success rate", `${successRate.toFixed(1)}%`, "all runs", "circle-check-big")}
      </div>`;
  }

  function successRateFromRuns() {
    if (!state.runs.length) return 0;
    const successful = state.runs.filter((run) => ["completed", "success", "succeeded", "passed"].includes(itemStatus(run))).length;
    return successful / state.runs.length;
  }

  function renderRecentRuns(runs, limit = 6) {
    const visible = runs.filter(matchesQuery).slice(0, limit);
    if (!visible.length) {
      if (state.errors.runs) return errorMarkup("runs", true);
      return emptyMarkup("No runs yet", "Launch the demo to execute the first workflow.", "activity", true);
    }
    return `
      <div class="data-table-wrap">
        <table class="data-table" aria-label="Recent agent runs">
          <thead><tr><th style="width:28%">Run</th><th style="width:18%">State</th><th style="width:18%">Agent</th><th style="width:14%">Cost</th><th style="width:22%">Started</th></tr></thead>
          <tbody>${visible.map((run) => {
            const id = itemId(run, "run");
            const name = first(run, ["name", "title", "objective", "task", "workflow_name"], `Run ${shortId(id, 7)}`);
            const agent = first(run, ["agent_name", "agent_id", "worker_id", "route"], "-");
            const cost = first(run, ["cost_usd", "cost"], 0);
            const started = first(run, ["started_at", "created_at", "updated_at"]);
            return `<tr tabindex="0" data-action="run-detail" data-id="${escapeAttribute(id)}">
              <td><span class="cell-main">${escapeHtml(name)}</span><span class="cell-sub mono">${escapeHtml(shortId(id))}</span></td>
              <td>${statusBadge(itemStatus(run))}</td>
              <td>${escapeHtml(shortId(agent, 13))}</td>
              <td class="numeric">${escapeHtml(formatUsd(cost))}</td>
              <td title="${escapeAttribute(formatDate(started))}">${escapeHtml(formatRelative(started))}</td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      </div>`;
  }

  function eventIconAndTone(event) {
    const type = String(first(event, ["type", "event_type", "name", "status"], "event")).toLowerCase();
    if (/fail|error|denied|reject/.test(type)) return ["circle-x", "error"];
    if (/approval|policy|pending|warning/.test(type)) return ["shield-alert", "warning"];
    if (/complete|success|pass|online/.test(type)) return ["circle-check", "success"];
    if (/start|run|workflow|agent/.test(type)) return ["activity", "info"];
    return ["radio", ""];
  }

  function renderActivity(events, limit = 7) {
    const visible = events.filter(matchesQuery).slice(0, limit);
    if (!visible.length) {
      if (state.errors.events) return errorMarkup("events", true);
      return emptyMarkup("No activity", "Runtime events will appear here as work executes.", "radio", true);
    }
    return `<ol class="activity-list">${visible.map((event) => {
      const [iconName, tone] = eventIconAndTone(event);
      const type = first(event, ["type", "event_type", "name"], "Runtime event");
      const payload = first(event, ["payload"], {});
      const payloadSummary = typeof payload === "object" ? first(payload, ["message", "description", "reason", "name", "agent_id"], "") : payload;
      const message = first(event, ["message", "description", "detail", "subject"], payloadSummary || first(event, ["run_id", "workflow_id", "agent_id"], "Event recorded"));
      const timestamp = first(event, ["timestamp", "created_at", "occurred_at", "time"]);
      return `<li class="activity-item">
        <span class="activity-icon ${tone}">${icon(iconName)}</span>
        <div class="activity-copy"><strong>${escapeHtml(String(type).replace(/[_\.]/g, " "))}</strong><span>${escapeHtml(message)}</span></div>
        <time datetime="${escapeAttribute(timestamp || "")}" title="${escapeAttribute(formatDate(timestamp))}">${escapeHtml(formatRelative(timestamp))}</time>
      </li>`;
    }).join("")}</ol>`;
  }

  function agentCapacity(agent) {
    const explicit = first(agent, ["load", "utilization", "utilization_pct", "capacity_used"]);
    if (explicit !== undefined) return normalizePercent(explicit);
    const active = numberValue(first(agent, ["active_runs", "inflight", "running_tasks"]));
    const capacity = numberValue(first(agent, ["capacity", "max_concurrency", "slots"]));
    return capacity ? clamp(active / capacity * 100) : (itemStatus(agent) === "offline" ? 0 : 24);
  }

  function renderWorkerFleet() {
    const counts = state.overview.counts || {};
    const online = numberValue(first(counts, ["online_workers"], state.agents.filter((agent) => itemStatus(agent) !== "offline").length));
    const active = state.agents.reduce((sum, agent) => sum + numberValue(first(agent, ["active_runs", "inflight", "running_tasks"])), 0);
    const capacity = state.agents.reduce((sum, agent) => sum + numberValue(first(agent, ["capacity", "max_concurrency", "slots"], 1)), 0);
    const visible = state.agents.filter(matchesQuery).slice(0, 5);
    return `
      <div class="fleet-summary">
        <div class="fleet-stat"><span>Online</span><strong>${formatNumber(online, 0)}</strong></div>
        <div class="fleet-stat"><span>Active tasks</span><strong>${formatNumber(active, 0)}</strong></div>
        <div class="fleet-stat"><span>Total slots</span><strong>${formatNumber(capacity || online, 0)}</strong></div>
      </div>
      ${visible.length ? `<div class="worker-list">${visible.map((agent) => {
        const load = agentCapacity(agent);
        const name = first(agent, ["name", "display_name"], shortId(itemId(agent, "agent")));
        const model = first(agent, ["model", "model_name", "provider"], "Model route");
        return `<div class="worker-row">
          <div class="worker-name"><strong>${escapeHtml(name)}</strong><span>${escapeHtml(model)}</span></div>
          ${statusBadge(itemStatus(agent, "online"))}
          <div class="worker-load"><div class="progress-track"><span style="width:${load}%"></span></div><span>${Math.round(load)}%</span></div>
        </div>`;
      }).join("")}</div>` : (state.errors.agents ? errorMarkup("agents", true) : emptyMarkup("No workers registered", "Agent workers will appear when connected.", "bot", true))}`;
  }

  function renderQualityPanel() {
    const metrics = state.overview.metrics || {};
    const quality = normalizePercent(first(metrics, ["quality", "quality_score", "eval_score"], 0));
    const success = normalizePercent(first(metrics, ["success_rate", "success"], successRateFromRuns()));
    const latency = numberValue(first(metrics, ["latency_ms", "p95_latency_ms", "latency"], averageRunMetric("latency_ms")));
    const tokens = numberValue(first(metrics, ["tokens", "token_count", "total_tokens"], state.runs.reduce((sum, run) => sum + numberValue(first(run, ["tokens", "token_count", "total_tokens"])), 0)));
    const latencyScore = latency ? clamp(100 - latency / 100) : 0;
    const tokenScore = tokens ? clamp(100 - Math.log10(Math.max(tokens, 1)) * 8) : 0;
    return `<div class="quality-layout">
      <div class="quality-score">
        <div class="score-ring" style="--score:${quality}" aria-label="Quality score ${quality.toFixed(1)} percent"><strong>${quality.toFixed(1)}</strong></div>
        <span>Composite quality score</span>
      </div>
      <div class="bar-list">
        ${qualityBar("Task success", success, `${success.toFixed(1)}%`, "")}
        ${qualityBar("Latency target", latencyScore, formatDuration(latency), "blue")}
        ${qualityBar("Token efficiency", tokenScore, formatNumber(tokens, 1), "amber")}
      </div>
    </div>`;
  }

  function qualityBar(label, value, display, tone) {
    return `<div class="bar-item">
      <div class="bar-item-head"><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong></div>
      <div class="progress-track ${safeClass(tone)}"><span style="width:${clamp(value)}%"></span></div>
    </div>`;
  }

  function averageRunMetric(key) {
    const values = state.runs.map((run) => numberValue(first(run, [key, key.replace("_ms", ""), `duration_${key}`]))).filter(Boolean);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
  }

  function workflowNodes(workflow) {
    const graph = workflow?.graph || {};
    const rawNodes = arrayValue(first(workflow, ["nodes", "tasks", "steps"], first(graph, ["nodes"], [])));
    return rawNodes.map((node, index) => {
      const id = String(first(node, ["node_id", "task_id", "step_id", "id", "name"], `node-${index + 1}`));
      let dependencies = first(node, ["depends_on", "dependencies", "deps", "requires", "parents"], []);
      if (!Array.isArray(dependencies)) dependencies = dependencies ? [dependencies] : [];
      dependencies = dependencies.map((dependency) => typeof dependency === "object" ? String(first(dependency, ["node_id", "id", "name"], "")) : String(dependency)).filter(Boolean);
      if (!dependencies.length && index > 0 && !rawNodes.some((candidate) => first(candidate, ["depends_on", "dependencies", "deps", "requires", "parents"]))) {
        dependencies = [String(first(rawNodes[index - 1], ["node_id", "task_id", "step_id", "id", "name"], `node-${index}`))];
      }
      return { ...node, _id: id, _dependencies: dependencies, _index: index };
    });
  }

  function nodeStages(nodes) {
    const byId = new Map(nodes.map((node) => [node._id, node]));
    const cache = new Map();
    function level(node, trail = new Set()) {
      if (cache.has(node._id)) return cache.get(node._id);
      if (trail.has(node._id)) return 0;
      const nextTrail = new Set(trail).add(node._id);
      const dependencyLevels = node._dependencies.map((id) => byId.get(id)).filter(Boolean).map((dependency) => level(dependency, nextTrail));
      const result = dependencyLevels.length ? Math.max(...dependencyLevels) + 1 : 0;
      cache.set(node._id, result);
      return result;
    }
    const stages = [];
    nodes.forEach((node) => {
      const index = level(node);
      if (!stages[index]) stages[index] = [];
      stages[index].push(node);
    });
    return stages.filter(Boolean);
  }

  function workflowProgress(workflow) {
    const explicit = first(workflow, ["progress", "progress_pct", "completion"]);
    if (explicit !== undefined) return normalizePercent(explicit);
    const nodes = workflowNodes(workflow);
    if (!nodes.length) {
      const counts = workflow?.node_counts || {};
      const total = numberValue(first(workflow, ["node_total", "node_count", "task_count"], Object.values(counts).reduce((sum, value) => sum + numberValue(value), 0)));
      const done = ["completed", "success", "succeeded", "passed", "approved"].reduce((sum, status) => sum + numberValue(counts[status]), 0);
      if (total) return clamp(done / total * 100);
      return ["completed", "success", "succeeded"].includes(itemStatus(workflow)) ? 100 : 0;
    }
    const done = nodes.filter((node) => ["completed", "success", "succeeded", "passed", "approved"].includes(itemStatus(node))).length;
    return clamp(done / nodes.length * 100);
  }

  function renderWorkflowExecution(workflows) {
    const visible = workflows.filter(matchesQuery).slice(0, 5);
    if (!visible.length) {
      if (state.errors.workflows) return errorMarkup("workflows", true);
      return emptyMarkup("No workflows", "Create or launch a workflow to see its DAG progress.", "workflow", true);
    }
    return `<div class="data-table-wrap"><table class="data-table" aria-label="Active workflow execution">
      <thead><tr><th style="width:25%">Workflow</th><th style="width:15%">State</th><th style="width:26%">DAG progress</th><th style="width:16%">Budget</th><th style="width:18%">Updated</th></tr></thead>
      <tbody>${visible.map((workflow) => {
        const id = itemId(workflow, "workflow");
        const name = first(workflow, ["name", "title"], `Workflow ${shortId(id, 7)}`);
        const progress = workflowProgress(workflow);
        const budget = workflow.budget || {};
        const spend = first(budget, ["spent_cost_usd", "cost_usd", "spent"], first(workflow, ["cost_usd", "spent_usd"], 0));
        const limit = first(budget, ["max_cost_usd", "cost_limit_usd", "limit"], first(workflow, ["budget_usd"], 0));
        const updated = first(workflow, ["updated_at", "created_at", "started_at"]);
        return `<tr tabindex="0" data-action="workflow-open" data-id="${escapeAttribute(id)}">
          <td><span class="cell-main">${escapeHtml(name)}</span><span class="cell-sub mono">${escapeHtml(shortId(id))}</span></td>
          <td>${statusBadge(itemStatus(workflow))}</td>
          <td><div class="bar-item-head"><span>${workflowNodes(workflow).length || first(workflow, ["node_total", "node_count", "task_count"], "-")} nodes</span><strong>${Math.round(progress)}%</strong></div><div class="progress-track"><span style="width:${progress}%"></span></div></td>
          <td class="numeric">${escapeHtml(formatUsd(spend))}${limit ? ` / ${escapeHtml(formatUsd(limit))}` : ""}</td>
          <td title="${escapeAttribute(formatDate(updated))}">${escapeHtml(formatRelative(updated))}</td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>`;
  }

  function renderOverview() {
    const recentRuns = arrayValue(state.overview.recent_runs).length ? arrayValue(state.overview.recent_runs) : state.runs;
    const workflows = arrayValue(state.overview.workflows).length ? arrayValue(state.overview.workflows) : state.workflows;
    const events = arrayValue(state.overview.events).length ? arrayValue(state.overview.events) : state.events;
    elements.overviewContent.innerHTML = `
      ${overviewMetrics()}
      <div class="dashboard-grid">
        <section class="panel">
          <header class="panel-header"><div class="panel-heading-copy"><h2>Recent runs</h2><p>Latest durable execution records</p></div><button class="quiet-button" type="button" data-action="navigate" data-view="runs">View all ${icon("arrow-right")}</button></header>
          <div class="panel-body flush">${renderRecentRuns(recentRuns)}</div>
        </section>
        <section class="panel">
          <header class="panel-header"><div class="panel-heading-copy"><h2>Live activity</h2><p>Runtime and governance events</p></div><span class="status-badge active">Live</span></header>
          <div class="panel-body flush">${renderActivity(events, 5)}</div>
        </section>
      </div>
      <div class="dashboard-grid balanced">
        <section class="panel">
          <header class="panel-header"><div class="panel-heading-copy"><h2>Cost and quality</h2><p>Workspace aggregate for the current window</p></div><span class="soft-badge teal">Evaluation signal</span></header>
          <div class="panel-body">${renderQualityPanel()}</div>
        </section>
        <section class="panel">
          <header class="panel-header"><div class="panel-heading-copy"><h2>Worker fleet</h2><p>Availability and capacity by route</p></div><button class="quiet-button" type="button" data-action="navigate" data-view="agents">Inspect ${icon("arrow-right")}</button></header>
          <div class="panel-body flush">${renderWorkerFleet()}</div>
        </section>
      </div>
      <section class="panel">
        <header class="panel-header"><div class="panel-heading-copy"><h2>Workflow execution</h2><p>DAG completion, state, and budget consumption</p></div><button class="quiet-button" type="button" data-action="navigate" data-view="workflows">Open orchestrator ${icon("arrow-right")}</button></header>
        <div class="panel-body flush">${renderWorkflowExecution(workflows)}</div>
      </section>`;
  }

  function runRows(runs) {
    return runs.map((run) => {
      const id = itemId(run, "run");
      const name = first(run, ["name", "title", "objective", "task", "workflow_name"], `Run ${shortId(id, 7)}`);
      const workflow = first(run, ["workflow_name", "workflow_id"], "-");
      const agent = first(run, ["agent_name", "agent_id", "worker_id", "route"], "-");
      const latency = first(run, ["latency_ms", "duration_ms", "elapsed_ms"], 0);
      const cost = first(run, ["cost_usd", "cost"], 0);
      const created = first(run, ["started_at", "created_at", "updated_at"]);
      return `<tr tabindex="0" data-action="run-detail" data-id="${escapeAttribute(id)}">
        <td><span class="cell-main">${escapeHtml(name)}</span><span class="cell-sub mono">${escapeHtml(shortId(id, 14))}</span></td>
        <td>${statusBadge(itemStatus(run))}</td>
        <td title="${escapeAttribute(workflow)}">${escapeHtml(shortId(workflow, 16))}</td>
        <td title="${escapeAttribute(agent)}">${escapeHtml(shortId(agent, 16))}</td>
        <td class="numeric">${escapeHtml(formatDuration(latency))}</td>
        <td class="numeric">${escapeHtml(formatUsd(cost))}</td>
        <td title="${escapeAttribute(formatDate(created))}">${escapeHtml(formatRelative(created))}</td>
      </tr>`;
    }).join("");
  }

  function renderRuns() {
    let runs = state.runs.filter(matchesQuery);
    if (state.runFilter !== "all") {
      const completed = ["completed", "success", "succeeded", "passed"];
      const active = ["running", "active", "in_progress", "queued", "pending"];
      runs = runs.filter((run) => state.runFilter === "completed" ? completed.includes(itemStatus(run)) : state.runFilter === "active" ? active.includes(itemStatus(run)) : ["failed", "error", "cancelled"].includes(itemStatus(run)));
    }
    const body = runs.length ? `<div class="data-table-wrap"><table class="data-table" aria-label="Agent runs">
      <thead><tr><th style="width:22%">Run</th><th style="width:12%">State</th><th style="width:15%">Workflow</th><th style="width:15%">Agent</th><th style="width:11%">Latency</th><th style="width:10%">Cost</th><th style="width:15%">Started</th></tr></thead>
      <tbody>${runRows(runs)}</tbody></table></div>` : (state.errors.runs ? errorMarkup("runs") : emptyMarkup(state.query || state.runFilter !== "all" ? "No matching runs" : "No runs yet", state.query || state.runFilter !== "all" ? "Change the filter or search query." : "Launch the demo to create a complete execution trace.", "activity"));
    elements.runsContent.innerHTML = `<section class="panel">
      <header class="panel-header">
        <div class="panel-heading-copy"><h2>Execution history</h2><p>${formatNumber(runs.length, 0)} visible records</p></div>
        <div class="segmented-control" role="group" aria-label="Filter runs">
          ${["all", "active", "completed", "failed"].map((filter) => `<button class="segment${state.runFilter === filter ? " active" : ""}" type="button" data-action="run-filter" data-filter="${filter}">${filter[0].toUpperCase() + filter.slice(1)}</button>`).join("")}
        </div>
      </header>
      <div class="panel-body flush">${body}</div>
      ${state.lastUpdatedAt ? `<footer class="panel-foot">Updated ${escapeHtml(formatRelative(state.lastUpdatedAt))} in ${escapeHtml(currentWorkspaceName())}</footer>` : ""}
    </section>`;
  }

  function selectedWorkflow() {
    const summary = state.workflows.find((workflow) => itemId(workflow, "workflow") === state.selectedWorkflowId);
    return state.workflowDetails.get(state.selectedWorkflowId) || summary || null;
  }

  async function loadWorkflowDetail(id, notify = true) {
    if (!id || state.workflowDetails.has(id)) return;
    try {
      const detail = await apiRequest(`/workflows/${encodeURIComponent(id)}`);
      state.workflowDetails.set(id, detail);
      if (state.view === "workflows" && state.selectedWorkflowId === id) renderWorkflows();
    } catch (error) {
      if (notify) showToast("Workflow details unavailable", error.message, "error");
    }
  }

  function renderWorkflowList(workflows) {
    if (!workflows.length) return state.errors.workflows ? errorMarkup("workflows", true) : emptyMarkup("No workflows", "Launch the demo to create the first durable DAG.", "workflow", true);
    return `<div class="workflow-list">${workflows.map((workflow) => {
      const id = itemId(workflow, "workflow");
      const name = first(workflow, ["name", "title"], `Workflow ${shortId(id, 7)}`);
      const nodes = workflowNodes(workflow);
      const updated = first(workflow, ["updated_at", "created_at", "started_at"]);
      return `<button class="workflow-list-item${id === state.selectedWorkflowId ? " selected" : ""}" type="button" data-action="workflow-select" data-id="${escapeAttribute(id)}">
        <strong>${escapeHtml(name)}</strong>${statusBadge(itemStatus(workflow))}
        <span class="workflow-list-meta"><span>${nodes.length || first(workflow, ["node_total", "node_count", "task_count"], 0)} nodes</span><time>${escapeHtml(formatRelative(updated))}</time></span>
      </button>`;
    }).join("")}</div>`;
  }

  function workflowApproval(workflow) {
    const id = itemId(workflow, "workflow");
    return state.approvals.find((approval) => String(first(approval, ["workflow_id", "resource_id"], "")) === id && ["pending", "requested", "open", "awaiting_approval"].includes(itemStatus(approval, "pending")));
  }

  function renderDagGraph(workflow) {
    const nodes = workflowNodes(workflow);
    if (!nodes.length) return emptyMarkup("DAG definition unavailable", "This workflow has no expanded nodes yet.", "git-branch", true);
    const stages = nodeStages(nodes);
    let displayIndex = 0;
    return `<div class="dag-graph" aria-label="Workflow dependency graph">${stages.map((stage, stageIndex) => `<div class="dag-stage" aria-label="Stage ${stageIndex + 1}">${stage.map((node) => {
      const status = itemStatus(node, stageIndex === 0 ? "ready" : "pending");
      const name = first(node, ["name", "title", "task", "type"], node._id);
      const description = first(node, ["description", "objective", "prompt", "action", "tool"], `${node._dependencies.length ? `Depends on ${node._dependencies.join(", ")}` : "Entry node"}`);
      const capabilities = Array.isArray(node.capabilities) ? node.capabilities.join(", ") : "";
      const worker = first(node, ["agent_name", "agent_id", "worker_id", "leased_by", "route"], capabilities || "unassigned");
      return `<article class="dag-node ${safeClass(status)}">
        <div class="dag-node-head"><div style="min-width:0"><strong>${escapeHtml(name)}</strong></div><span class="node-index">${++displayIndex}</span></div>
        <p>${escapeHtml(description)}</p>
        <div class="dag-node-meta"><span>${escapeHtml(shortId(worker, 12))}</span><span>${escapeHtml(status.replace(/_/g, " "))}</span></div>
      </article>`;
    }).join("")}</div>`).join("")}</div>`;
  }

  function budgetValue(workflow, keys, rootKeys, fallback = 0) {
    return first(workflow?.budget || {}, keys, first(workflow, rootKeys, fallback));
  }

  function renderBudgetPanel(workflow) {
    const nodes = workflowNodes(workflow);
    const nodeSum = (reader) => nodes.reduce((sum, node) => sum + numberValue(reader(node)), 0);
    const costUsed = numberValue(budgetValue(workflow, ["spent_cost_usd", "cost_usd", "spent"], ["cost_usd", "spent_usd"], nodeSum((node) => node.cost_usd)));
    const costLimit = numberValue(budgetValue(workflow, ["max_cost_usd", "cost_limit_usd", "limit"], ["budget_usd", "cost_limit_usd"], nodeSum((node) => first(node.budget || {}, ["cost_usd", "max_cost_usd"]))));
    const tokensUsed = numberValue(budgetValue(workflow, ["used_tokens", "tokens", "token_count"], ["tokens", "token_count"], nodeSum((node) => first(node, ["tokens_used", "tokens"]))));
    const tokenLimit = numberValue(budgetValue(workflow, ["max_tokens", "token_limit"], ["max_tokens", "token_budget"], nodeSum((node) => first(node.budget || {}, ["tokens", "max_tokens"]))));
    const timeUsed = numberValue(budgetValue(workflow, ["elapsed_ms", "duration_ms"], ["duration_ms", "latency_ms"], nodeSum((node) => numberValue(node.duration_seconds) * 1000)));
    const timeLimit = numberValue(budgetValue(workflow, ["timeout_ms", "max_duration_ms"], ["timeout_ms"], nodeSum((node) => numberValue(first(node.budget || {}, ["seconds"])) * 1000)));
    const item = (label, used, limit, formatter, tone = "") => {
      const percent = limit ? clamp(used / limit * 100) : 0;
      return `<div class="budget-item"><div class="budget-item-head"><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatter(used))}${limit ? ` / ${escapeHtml(formatter(limit))}` : ""}</strong></div><div class="progress-track ${tone}"><span style="width:${percent}%"></span></div></div>`;
    };
    return `<div class="budget-panel">${item("Cost budget", costUsed, costLimit, formatUsd)}${item("Token budget", tokensUsed, tokenLimit, (value) => formatNumber(value, 1), "blue")}${item("Time budget", timeUsed, timeLimit, formatDuration, "amber")}</div>`;
  }

  function renderWorkflowDetail(workflow) {
    if (!workflow) return emptyMarkup("Select a workflow", "Choose an orchestration record to inspect its graph.", "mouse-pointer-click", true);
    const id = itemId(workflow, "workflow");
    const name = first(workflow, ["name", "title"], `Workflow ${shortId(id, 7)}`);
    const nodes = workflowNodes(workflow);
    const progress = workflowProgress(workflow);
    const approval = workflowApproval(workflow);
    const updated = first(workflow, ["updated_at", "created_at", "started_at"]);
    return `
      <div class="workflow-summary">
        <div><span>Status</span><strong>${statusBadge(itemStatus(workflow))}</strong></div>
        <div><span>DAG completion</span><strong>${Math.round(progress)}% / ${nodes.length || "-"} nodes</strong></div>
        <div><span>Approval gate</span><strong>${approval ? "Review required" : "No pending gate"}</strong></div>
        <div><span>Last update</span><strong title="${escapeAttribute(formatDate(updated))}">${escapeHtml(formatRelative(updated))}</strong></div>
      </div>
      <div class="dag-canvas">${renderDagGraph(workflow)}</div>
      ${renderBudgetPanel(workflow)}
      ${approval ? `<div class="panel-foot" style="display:flex;align-items:center;gap:8px;color:var(--amber)">${icon("shield-alert")} Approval ${escapeHtml(shortId(itemId(approval, "approval")))} is blocking this workflow. <button class="quiet-button" type="button" data-action="navigate" data-view="approvals">Review gate</button></div>` : ""}`;
  }

  function renderWorkflows() {
    const workflows = state.workflows.filter(matchesQuery);
    if (workflows.length && !workflows.some((workflow) => itemId(workflow, "workflow") === state.selectedWorkflowId)) {
      state.selectedWorkflowId = itemId(workflows[0], "workflow");
    }
    const selected = selectedWorkflow();
    elements.workflowsContent.innerHTML = `<div class="workflow-layout">
      <section class="panel">
        <header class="panel-header"><div class="panel-heading-copy"><h2>Workflow registry</h2><p>${formatNumber(workflows.length, 0)} orchestration records</p></div></header>
        <div class="panel-body flush">${renderWorkflowList(workflows)}</div>
      </section>
      <section class="panel">
        <header class="panel-header"><div class="panel-heading-copy"><h2>${escapeHtml(selected ? first(selected, ["name", "title"], `Workflow ${shortId(itemId(selected, "workflow"), 7)}`) : "Execution graph")}</h2><p class="mono">${escapeHtml(selected ? itemId(selected, "workflow") : "No workflow selected")}</p></div>${selected ? statusBadge(itemStatus(selected)) : ""}</header>
        <div class="panel-body flush">${renderWorkflowDetail(selected)}</div>
      </section>
    </div>`;
  }

  function renderAgents() {
    const agents = state.agents.filter(matchesQuery);
    if (!agents.length) {
      elements.agentsContent.innerHTML = state.errors.agents ? errorMarkup("agents") : emptyMarkup(state.query ? "No matching agents" : "No agents registered", state.query ? "Try a broader search query." : "Connect a worker to register it with the control plane.", "bot");
      return;
    }
    elements.agentsContent.innerHTML = `<div class="agent-grid">${agents.map((agent) => {
      const id = itemId(agent, "agent");
      const name = first(agent, ["name", "display_name"], `Agent ${shortId(id, 7)}`);
      const model = first(agent, ["model", "model_name", "provider"], "Dynamic model route");
      const description = first(agent, ["description", "role", "purpose", "system_prompt"], "Specialized agent worker managed by the control plane.");
      const runs = first(agent, ["runs", "run_count", "total_runs"], 0);
      const quality = normalizePercent(first(agent, ["quality", "quality_score", "success_rate"], 0));
      const active = first(agent, ["active_runs", "inflight", "running_tasks"], 0);
      const initial = String(name).trim().charAt(0).toUpperCase() || "A";
      return `<article class="agent-card">
        <div class="agent-card-head">
          <div class="agent-identity"><span class="agent-avatar">${escapeHtml(initial)}</span><div><strong>${escapeHtml(name)}</strong><span class="mono">${escapeHtml(shortId(id, 16))}</span></div></div>
          ${statusBadge(itemStatus(agent, "online"))}
        </div>
        <p class="agent-description">${escapeHtml(String(description).slice(0, 150))}</p>
        <span class="soft-badge teal">${escapeHtml(model)}</span>
        <div class="agent-stats"><div class="agent-stat"><span>Runs</span><strong>${formatNumber(runs, 0)}</strong></div><div class="agent-stat"><span>Quality</span><strong>${quality.toFixed(1)}%</strong></div><div class="agent-stat"><span>Active</span><strong>${formatNumber(active, 0)}</strong></div></div>
      </article>`;
    }).join("")}</div>`;
  }

  function skillTrust(skill) {
    const explicit = first(skill, ["status", "trust", "trust_status", "verification_status"]);
    if (explicit) return String(explicit).toLowerCase();
    if (first(skill, ["verified", "signature_valid", "signed"], false)) return "verified";
    return "unverified";
  }

  function renderSkills() {
    const skills = state.skills.filter(matchesQuery);
    const content = skills.length ? `<div class="data-table-wrap"><table class="data-table" aria-label="Skill registry">
      <thead><tr><th style="width:25%">Skill</th><th style="width:12%">Version</th><th style="width:15%">Trust</th><th style="width:17%">Publisher</th><th style="width:15%">Eval score</th><th style="width:16%">Updated</th></tr></thead>
      <tbody>${skills.map((skill) => {
        const id = itemId(skill, "skill");
        const name = first(skill, ["name", "display_name", "slug"], shortId(id));
        const description = first(skill, ["description", "summary"], id);
        const version = first(skill, ["version", "latest_version", "release"], "-");
        const publisher = first(skill, ["publisher", "author", "owner"], "workspace");
        const score = normalizePercent(first(skill, ["evaluation_score", "eval_score", "quality", "score"], 0));
        const updated = first(skill, ["updated_at", "published_at", "created_at"]);
        return `<tr>
          <td><span class="cell-main">${escapeHtml(name)}</span><span class="cell-sub">${escapeHtml(String(description).slice(0, 80))}</span></td>
          <td class="mono">${escapeHtml(version)}</td>
          <td>${statusBadge(skillTrust(skill))}</td>
          <td>${escapeHtml(publisher)}</td>
          <td><div class="bar-item-head"><span>${score ? "Evaluated" : "No result"}</span><strong>${score.toFixed(1)}</strong></div><div class="progress-track"><span style="width:${score}%"></span></div></td>
          <td title="${escapeAttribute(formatDate(updated))}">${escapeHtml(formatRelative(updated))}</td>
        </tr>`;
      }).join("")}</tbody></table></div>` : (state.errors.skills ? errorMarkup("skills") : emptyMarkup(state.query ? "No matching skills" : "Registry is empty", state.query ? "Try another name, publisher, or version." : "Publish a signed skill package to make it available to agents.", "blocks"));
    elements.skillsContent.innerHTML = `<section class="panel"><header class="panel-header"><div class="panel-heading-copy"><h2>Trusted capability registry</h2><p>${formatNumber(skills.length, 0)} packages visible</p></div><span class="soft-badge teal">Signed supply chain</span></header><div class="panel-body flush">${content}</div></section>`;
  }

  function approvalIsPending(approval) {
    return ["pending", "requested", "open", "awaiting_approval"].includes(itemStatus(approval, "pending"));
  }

  function renderApprovals() {
    let approvals = state.approvals.filter(matchesQuery);
    if (state.approvalFilter === "pending") approvals = approvals.filter(approvalIsPending);
    if (state.approvalFilter === "resolved") approvals = approvals.filter((approval) => !approvalIsPending(approval));
    const content = approvals.length ? `<div class="approval-list">${approvals.map((approval) => {
      const id = itemId(approval, "approval");
      const risk = String(first(approval, ["risk", "risk_level", "severity"], "medium")).toLowerCase();
      const title = first(approval, ["title", "action", "tool_name", "operation", "reason"], `Approval ${shortId(id, 7)}`);
      const reason = first(approval, ["description", "reason", "message", "policy"], "A human decision is required before execution can continue.");
      const requester = first(approval, ["requester", "requested_by", "agent_name", "agent_id"], "agent runtime");
      const workflow = first(approval, ["workflow_name", "workflow_id", "run_id", "resource_id"], "No workflow context");
      const pending = approvalIsPending(approval);
      const busy = state.approvalPending.has(id);
      return `<article class="approval-row">
        <span class="approval-risk-icon ${safeClass(risk)}">${icon(risk === "low" ? "shield-check" : "shield-alert")}</span>
        <div class="approval-copy"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(String(reason).slice(0, 180))}</span><div style="margin-top:7px">${riskBadge(risk)} ${statusBadge(itemStatus(approval, "pending"))}</div></div>
        <div class="approval-context"><span>Requested by</span><strong>${escapeHtml(shortId(requester, 18))}</strong><span style="margin-top:5px">Workflow</span><strong title="${escapeAttribute(workflow)}">${escapeHtml(shortId(workflow, 18))}</strong></div>
        <div class="approval-actions">${pending ? `<button class="secondary-button" type="button" data-action="approval" data-id="${escapeAttribute(id)}" data-approved="false"${busy ? " disabled" : ""}>${icon("x")} Deny</button><button class="primary-button" type="button" data-action="approval" data-id="${escapeAttribute(id)}" data-approved="true"${busy ? " disabled" : ""}>${icon("check")} Approve</button>` : `<span class="muted">Resolved ${escapeHtml(formatRelative(first(approval, ["resolved_at", "updated_at"])))}</span>`}</div>
      </article>`;
    }).join("")}</div>` : (state.errors.approvals ? errorMarkup("approvals") : emptyMarkup(state.query || state.approvalFilter !== "all" ? "No matching approvals" : "Approval queue is clear", state.query ? "Try another search query." : state.approvalFilter === "pending" ? "No policy gates require operator action." : "Decisions will appear here after review.", "shield-check"));
    elements.approvalsContent.innerHTML = `<section class="panel">
      <header class="panel-header"><div class="panel-heading-copy"><h2>Decision queue</h2><p>${formatNumber(approvals.length, 0)} visible requests</p></div><div class="segmented-control" role="group" aria-label="Filter approvals">${["pending", "resolved", "all"].map((filter) => `<button class="segment${state.approvalFilter === filter ? " active" : ""}" type="button" data-action="approval-filter" data-filter="${filter}">${filter[0].toUpperCase() + filter.slice(1)}</button>`).join("")}</div></header>
      <div class="panel-body flush">${content}</div>
    </section>`;
  }

  function routeMetrics() {
    const overviewRoutes = arrayValue(first(state.overview, ["route_metrics", "routes", "models"], []));
    if (overviewRoutes.length) return overviewRoutes;
    const grouped = new Map();
    state.runs.forEach((run) => {
      const route = String(first(run, ["route", "model", "model_name", "provider"], "default"));
      if (!grouped.has(route)) grouped.set(route, { route, runs: 0, successes: 0, total_quality: 0, quality_count: 0 });
      const item = grouped.get(route);
      item.runs += 1;
      if (["completed", "success", "succeeded", "passed"].includes(itemStatus(run))) item.successes += 1;
      const quality = first(run, ["quality", "quality_score", "evaluation_score"]);
      if (quality !== undefined) { item.total_quality += normalizePercent(quality); item.quality_count += 1; }
    });
    return Array.from(grouped.values()).map((item) => ({ ...item, score: item.quality_count ? item.total_quality / item.quality_count / 100 : item.runs ? item.successes / item.runs : 0 }));
  }

  function renderEvaluations() {
    const metrics = state.overview.metrics || {};
    const routes = routeMetrics().filter(matchesQuery);
    const quality = normalizePercent(first(metrics, ["quality", "quality_score", "eval_score"], 0));
    const success = normalizePercent(first(metrics, ["success_rate", "success"], successRateFromRuns()));
    const latency = numberValue(first(metrics, ["latency_ms", "p95_latency_ms", "latency"], averageRunMetric("latency_ms")));
    const evaluatedSkills = state.skills.filter((skill) => first(skill, ["evaluation_score", "eval_score", "score"]) !== undefined);
    const evaluationRecords = state.evaluations.length
      ? state.evaluations.filter(matchesQuery)
      : evaluatedSkills.filter(matchesQuery);
    const routeList = routes.length ? `<div class="route-list">${routes.map((route) => {
      const name = first(route, ["route", "name", "pool", "model", "provider"], "default");
      const runs = numberValue(first(route, ["runs", "run_count", "samples", "total"]));
      const successes = numberValue(first(route, ["successes", "successful", "passed"]));
      const score = normalizePercent(first(route, ["score", "quality", "success_rate"], runs ? successes / runs : 0));
      return `<div class="route-row"><div class="route-name"><strong>${escapeHtml(name)}</strong><span>${formatNumber(runs, 0)} samples</span></div><div class="progress-track"><span style="width:${score}%"></span></div><div class="route-score">${score.toFixed(1)}</div></div>`;
    }).join("")}</div>` : emptyMarkup("No route metrics", "Evaluation results appear after agent runs are scored.", "flask-conical", true);
    const evaluationRows = evaluationRecords.slice(0, 8);
    const evaluationTable = evaluationRows.length ? `<div class="data-table-wrap"><table class="data-table" aria-label="Evaluation results"><thead><tr><th style="width:30%">Subject</th><th style="width:18%">Suite</th><th style="width:16%">Score</th><th style="width:16%">Gate</th><th style="width:20%">Evaluated</th></tr></thead><tbody>${evaluationRows.map((record) => {
      const id = first(record, ["evaluation_id", "id", "skill_id"], itemId(record, "skill"));
      const name = first(record, ["name", "display_name", "slug"], shortId(id));
      const suite = first(record, ["evaluation_suite", "eval_suite", "benchmark", "baseline"], "trace-conformance");
      const score = normalizePercent(first(record, ["evaluation_score", "eval_score", "score"], 0));
      const gate = first(record, ["status", "gate_status", "evaluation_status"], score >= 80 ? "passed" : "failed");
      const date = first(record, ["evaluated_at", "updated_at", "published_at", "created_at"]);
      const candidate = first(record, ["candidate", "version"], id);
      return `<tr><td><span class="cell-main">${escapeHtml(name)}</span><span class="cell-sub mono">${escapeHtml(shortId(candidate, 18))}</span></td><td>${escapeHtml(suite)}</td><td class="numeric">${score.toFixed(1)}</td><td>${statusBadge(gate)}</td><td title="${escapeAttribute(formatDate(date))}">${escapeHtml(formatRelative(date))}</td></tr>`;
    }).join("")}</tbody></table></div>` : emptyMarkup("No evaluation records", "Scores from the skill and run evaluation gates will appear here.", "clipboard-check", true);
    elements.evaluationsContent.innerHTML = `
      <div class="evaluation-grid">
        <section class="panel"><header class="panel-header"><div class="panel-heading-copy"><h2>Quality posture</h2><p>Current workspace aggregate</p></div></header><div class="evaluation-summary"><div class="evaluation-metric"><span>Quality</span><strong>${quality.toFixed(1)}</strong><small>composite score</small></div><div class="evaluation-metric"><span>Success</span><strong>${success.toFixed(1)}%</strong><small>all runs</small></div><div class="evaluation-metric"><span>P95 latency</span><strong>${escapeHtml(formatDuration(latency))}</strong><small>execution time</small></div><div class="evaluation-metric"><span>Evaluations</span><strong>${formatNumber(evaluationRecords.length, 0)}</strong><small>release gates</small></div></div></section>
        <section class="panel"><header class="panel-header"><div class="panel-heading-copy"><h2>Route performance</h2><p>Quality-weighted model and worker routing</p></div><span class="soft-badge teal">Adaptive routing</span></header><div class="panel-body">${routeList}</div></section>
      </div>
      <section class="panel"><header class="panel-header"><div class="panel-heading-copy"><h2>Evaluation ledger</h2><p>Conformance and regression gate outcomes</p></div></header><div class="panel-body flush">${evaluationTable}</div></section>`;
  }

  function renderCurrentView() {
    if (state.loading) {
      const target = elements[`${state.view}Content`];
      if (target) target.innerHTML = loadingMarkup();
      hydrateIcons();
      return;
    }
    const renderers = {
      overview: renderOverview,
      runs: renderRuns,
      workflows: renderWorkflows,
      agents: renderAgents,
      skills: renderSkills,
      approvals: renderApprovals,
      evaluations: renderEvaluations
    };
    renderers[state.view]?.();
    hydrateIcons();
  }

  function navigate(view, { updateHash = true } = {}) {
    if (!PAGE_META[view]) view = "overview";
    state.view = view;
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    $$('[data-view-panel]').forEach((panel) => {
      const active = panel.dataset.viewPanel === view;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    });
    const meta = PAGE_META[view];
    elements.pageEyebrow.textContent = meta.eyebrow;
    elements.pageTitle.textContent = meta.title;
    elements.pageDescription.textContent = meta.description;
    elements.globalSearch.placeholder = `Search ${view === "overview" ? "runs, agents, workflows" : view}...`;
    if (updateHash) history.replaceState(null, "", `#${view}`);
    document.body.classList.remove("sidebar-open");
    renderCurrentView();
  }

  function openRunDetail(run) {
    if (!run) return;
    const id = itemId(run, "run");
    const name = first(run, ["name", "title", "objective", "task", "workflow_name"], `Run ${shortId(id, 7)}`);
    const fields = [
      ["State", statusBadge(itemStatus(run)), true],
      ["Run ID", id],
      ["Workflow", first(run, ["workflow_name", "workflow_id"], "-")],
      ["Agent / worker", first(run, ["agent_name", "agent_id", "worker_id", "route"], "-")],
      ["Started", formatDate(first(run, ["started_at", "created_at"]))],
      ["Completed", formatDate(first(run, ["completed_at", "finished_at", "updated_at"]))],
      ["Latency", formatDuration(first(run, ["latency_ms", "duration_ms", "elapsed_ms"], 0))],
      ["Cost", formatUsd(first(run, ["cost_usd", "cost"], 0))],
      ["Tokens", formatNumber(first(run, ["tokens", "token_count", "total_tokens"], 0), 1)],
      ["Quality", `${normalizePercent(first(run, ["quality", "quality_score", "evaluation_score"], 0)).toFixed(1)}`]
    ];
    elements.dialogEyebrow.textContent = "Execution record";
    elements.dialogTitle.textContent = name;
    elements.dialogBody.innerHTML = `<div class="detail-grid">${fields.map(([label, value, raw]) => `<div class="detail-field"><span>${escapeHtml(label)}</span><strong>${raw ? value : escapeHtml(value)}</strong></div>`).join("")}</div><h3 class="dialog-section-title">Raw trace summary</h3><pre class="code-block">${escapeHtml(JSON.stringify(run, null, 2))}</pre>`;
    elements.dialogFooter.innerHTML = `<button class="secondary-button" type="submit">Close</button>`;
    showDialog();
  }

  function showDialog() {
    hydrateIcons();
    if (typeof elements.detailDialog.showModal === "function") elements.detailDialog.showModal();
    else elements.detailDialog.setAttribute("open", "");
  }

  async function handleApproval(id, approved) {
    if (!id || state.approvalPending.has(id)) return;
    state.approvalPending.add(id);
    renderApprovals();
    const actor = localStorage.getItem("evoagent.actor") || "control-plane-operator";
    try {
      const updated = await apiRequest(`/approvals/${encodeURIComponent(id)}`, {
        method: "POST",
        body: JSON.stringify({ approved, actor })
      });
      const index = state.approvals.findIndex((approval) => itemId(approval, "approval") === id);
      if (index >= 0) state.approvals[index] = { ...state.approvals[index], ...updated, status: first(updated, ["status"], approved ? "approved" : "denied") };
      showToast(approved ? "Approval granted" : "Request denied", `Decision recorded for ${shortId(id, 14)}`);
      updateNavCounts();
      await refreshEndpoints(["overview", "approvals", "workflows", "events"]);
    } catch (error) {
      showToast("Decision failed", error.message, "error");
    } finally {
      state.approvalPending.delete(id);
      if (state.view === "approvals") renderApprovals();
    }
  }

  async function refreshEndpoints(endpoints) {
    const settled = await Promise.allSettled(endpoints.map(async (endpoint) => ({ endpoint, payload: await apiRequest(`/${endpoint}`) })));
    settled.forEach((result) => {
      if (result.status === "fulfilled") {
        const { endpoint, payload } = result.value;
        state[endpoint] = endpoint === "overview" ? payload : arrayValue(payload);
        delete state.errors[endpoint];
      }
    });
    updateNavCounts();
    renderCurrentView();
  }

  async function launchDemo() {
    if (elements.launchDemo.disabled) return;
    elements.launchDemo.disabled = true;
    const original = elements.launchDemo.innerHTML;
    elements.launchDemo.innerHTML = `${icon("loader-circle")}<span>Launching...</span>`;
    $("svg", elements.launchDemo)?.classList.add("spin");
    hydrateIcons();
    try {
      const payload = await apiRequest("/demo/launch", { method: "POST" });
      const id = first(payload, ["workflow_id", "run_id", "id"], "");
      showToast("Demo launched", id ? `Execution ${shortId(id, 16)} is now active` : "The reference workflow is now active");
      await refreshEndpoints(["overview", "runs", "workflows", "approvals", "events"]);
      navigate("overview");
    } catch (error) {
      showToast("Demo launch failed", error.message, "error");
    } finally {
      elements.launchDemo.disabled = false;
      elements.launchDemo.innerHTML = original;
      hydrateIcons();
    }
  }

  function handleContentAction(event) {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const action = target.dataset.action;
    if (action === "navigate") navigate(target.dataset.view);
    if (action === "retry") loadAll({ notify: true });
    if (action === "run-filter") { state.runFilter = target.dataset.filter; renderRuns(); hydrateIcons(); }
    if (action === "approval-filter") { state.approvalFilter = target.dataset.filter; renderApprovals(); hydrateIcons(); }
    if (action === "run-detail") openRunDetail(state.runs.find((run) => itemId(run, "run") === target.dataset.id) || arrayValue(state.overview.recent_runs).find((run) => itemId(run, "run") === target.dataset.id));
    if (action === "workflow-open") { state.selectedWorkflowId = target.dataset.id; navigate("workflows"); loadWorkflowDetail(target.dataset.id); }
    if (action === "workflow-select") { state.selectedWorkflowId = target.dataset.id; renderWorkflows(); hydrateIcons(); loadWorkflowDetail(target.dataset.id); }
    if (action === "approval") handleApproval(target.dataset.id, target.dataset.approved === "true");
    if (action === "dismiss-toast") {
      const toast = target.closest(".toast");
      if (toast?._timer) clearTimeout(toast._timer);
      toast?.remove();
    }
  }

  function handleKeyboardActivation(event) {
    if ((event.key === "Enter" || event.key === " ") && event.target.matches('tr[data-action]')) {
      event.preventDefault();
      event.target.click();
    }
  }

  function bindEvents() {
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.view)));
    elements.openSidebar.addEventListener("click", () => document.body.classList.add("sidebar-open"));
    elements.closeSidebar.addEventListener("click", () => document.body.classList.remove("sidebar-open"));
    elements.sidebarScrim.addEventListener("click", () => document.body.classList.remove("sidebar-open"));
    elements.refreshButton.addEventListener("click", () => loadAll({ notify: true }));
    elements.bannerRetry.addEventListener("click", () => loadAll({ notify: true }));
    elements.launchDemo.addEventListener("click", launchDemo);
    elements.workspaceSelect.addEventListener("change", () => {
      state.workspaceId = elements.workspaceSelect.value;
      state.selectedWorkflowId = "";
      state.workflowDetails.clear();
      loadAll({ notify: false });
    });
    elements.globalSearch.addEventListener("input", () => {
      state.query = elements.globalSearch.value.trim();
      renderCurrentView();
    });
    document.addEventListener("click", handleContentAction);
    document.addEventListener("keydown", (event) => {
      handleKeyboardActivation(event);
      if (event.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) {
        event.preventDefault();
        elements.globalSearch.focus();
      }
      if (event.key === "Escape") document.body.classList.remove("sidebar-open");
    });
    window.addEventListener("hashchange", () => navigate(location.hash.slice(1), { updateHash: false }));
  }

  function cacheElements() {
    Object.assign(elements, {
      workspaceSelect: $("#workspace-select"),
      globalSearch: $("#global-search"),
      apiStatus: $("#api-status"),
      refreshButton: $("#refresh-button"),
      launchDemo: $("#launch-demo"),
      openSidebar: $("#open-sidebar"),
      closeSidebar: $("#close-sidebar"),
      sidebarScrim: $("#sidebar-scrim"),
      sidebarHealthDot: $("#sidebar-health-dot"),
      sidebarHealthLabel: $("#sidebar-health-label"),
      sidebarHealthMeta: $("#sidebar-health-meta"),
      pageEyebrow: $("#page-eyebrow"),
      pageTitle: $("#page-title"),
      pageDescription: $("#page-description"),
      connectionBanner: $("#connection-banner"),
      connectionError: $("#connection-error"),
      bannerRetry: $("#banner-retry"),
      overviewContent: $("#overview-content"),
      runsContent: $("#runs-content"),
      workflowsContent: $("#workflows-content"),
      agentsContent: $("#agents-content"),
      skillsContent: $("#skills-content"),
      approvalsContent: $("#approvals-content"),
      evaluationsContent: $("#evaluations-content"),
      navRuns: $("#nav-runs"),
      navWorkflows: $("#nav-workflows"),
      navAgents: $("#nav-agents"),
      navSkills: $("#nav-skills"),
      navApprovals: $("#nav-approvals"),
      toastRegion: $("#toast-region"),
      detailDialog: $("#detail-dialog"),
      dialogEyebrow: $("#dialog-eyebrow"),
      dialogTitle: $("#dialog-title"),
      dialogBody: $("#dialog-body"),
      dialogFooter: $("#dialog-footer")
    });
  }

  function init() {
    cacheElements();
    bindEvents();
    const initialView = location.hash.slice(1);
    navigate(PAGE_META[initialView] ? initialView : "overview", { updateHash: false });
    hydrateIcons();
    loadAll();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
