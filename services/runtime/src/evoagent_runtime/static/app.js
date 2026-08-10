const state = { after: 0, token: localStorage.getItem("evoagent-token") || "" };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
const headers = () => state.token ? {"Authorization": `Bearer ${state.token}`, "Content-Type": "application/json"} : {"Content-Type": "application/json"};
const toast = (message) => { const el = $("#toast"); el.textContent = message; el.classList.add("show"); setTimeout(() => el.classList.remove("show"), 2600); };
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {...headers(), ...(options.headers || {})}});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
  return response.json();
}
function renderRuns(runs) {
  $("#metric-runs").textContent = runs.length;
  $("#run-count").textContent = `${runs.length} rows`;
  $("#runs").innerHTML = runs.map((run) => `<tr>
    <td><span class="state ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span></td>
    <td title="${escapeHtml(run.run_id)}"><code>${escapeHtml(run.run_id)}</code></td>
    <td title="${escapeHtml(run.session_id)}"><code>${escapeHtml(run.session_id)}</code></td>
    <td title="${escapeHtml(run.input_text)}">${escapeHtml(run.input_text)}</td>
    <td title="${escapeHtml(run.output_text || run.error || "")}">${escapeHtml(run.output_text || run.error || "")}</td>
    <td>${new Date(run.updated_at).toLocaleString()}</td>
  </tr>`).join("");
}
function renderApprovals(items) {
  $("#metric-approvals").textContent = items.length;
  $("#approvals").innerHTML = items.length ? items.map((item) => `<article class="approval">
    <header><code>${escapeHtml(item.tool_name)}</code><span class="state awaiting_approval">pending</span></header>
    <p>${escapeHtml(item.reason)}</p>
    <div class="approval-actions"><button class="approve" data-id="${escapeHtml(item.approval_id)}" data-decision="true">Approve</button><button class="deny" data-id="${escapeHtml(item.approval_id)}" data-decision="false">Deny</button></div>
  </article>`).join("") : '<div class="empty">No pending approvals</div>';
}
function renderEvents(items) {
  if (items.length) state.after = items[items.length - 1].seq;
  $("#metric-events").textContent = state.after;
  $("#events").innerHTML = items.slice().reverse().map((item) => `<li><time>${new Date(item.created_at).toLocaleTimeString()}</time><strong>${escapeHtml(item.kind)}</strong><code>${escapeHtml(JSON.stringify(item.payload))}</code></li>`).join("");
}
function renderCandidates(items) {
  $("#candidates").innerHTML = items.map((item) => `<tr><td><span class="state ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td><td><code>${escapeHtml(item.candidate_id)}</code></td><td>v${item.parent_version}</td><td>${item.baseline_score == null ? "-" : Number(item.baseline_score).toFixed(3)}</td><td>${item.candidate_score == null ? "-" : Number(item.candidate_score).toFixed(3)}</td><td>${item.safety == null ? "-" : Number(item.safety).toFixed(3)}</td></tr>`).join("");
}
async function refresh() {
  try {
    const [health, runs, approvals, events, candidates] = await Promise.all([
      api("/health"), api("/v1/runs"), api("/v1/approvals"), api("/v1/events?limit=100"), api("/v1/evolution/candidates")
    ]);
    $("#status-dot").classList.add("ok");
    $("#status-label").textContent = "Online";
    $("#provider-label").textContent = health.provider;
    $("#metric-prompt").textContent = `v${health.prompt_version}`;
    renderRuns(runs); renderApprovals(approvals); renderEvents(events); renderCandidates(candidates);
  } catch (error) {
    $("#status-dot").classList.remove("ok"); $("#status-label").textContent = "Unavailable"; toast(error.message);
  }
}
document.addEventListener("DOMContentLoaded", () => {
  $("#gateway-token").value = state.token;
  $("#gateway-token").addEventListener("change", (event) => { state.token = event.target.value; localStorage.setItem("evoagent-token", state.token); refresh(); });
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item,.view").forEach((el) => el.classList.remove("active"));
    button.classList.add("active"); $(`#${button.dataset.view}`).classList.add("active");
  }));
  $("#message-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { const result = await api("/v1/messages", {method:"POST", body:JSON.stringify({channel:$("#channel").value, peer_id:$("#peer").value, text:$("#message").value})}); toast(`Accepted ${result.run_id}`); setTimeout(refresh, 400); } catch (error) { toast(error.message); }
  });
  $("#approvals").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-id]"); if (!button) return;
    try { await api(`/v1/approvals/${button.dataset.id}`, {method:"POST", body:JSON.stringify({approved:button.dataset.decision === "true", actor:"control-ui"})}); toast("Decision recorded"); refresh(); } catch (error) { toast(error.message); }
  });
  $("#propose").addEventListener("click", async () => { try { await api("/v1/evolution/candidates", {method:"POST", body:"{}"}); toast("Candidate proposed"); refresh(); } catch (error) { toast(error.message); } });
  $("#refresh").addEventListener("click", refresh); $("#approvals-refresh").addEventListener("click", refresh);
  $("#clock").textContent = new Date().toLocaleString();
  if (window.lucide) window.lucide.createIcons();
  refresh(); setInterval(refresh, 3000);
});
