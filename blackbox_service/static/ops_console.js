/* ============================================================
   Operations Console — JavaScript
   Drives the Phase-A SSE live view.
   ============================================================ */

"use strict";

/* ── Event → UI mapping ─────────────────────────────────────────────────────
   One entry per event type. Adding a new event type = one line here.
   Fields:
     label            chip text (required)
     color            CSS color string (required)
     okFalseLabel     override label when payload.ok === false
     okFalseColor     override color when payload.ok === false
     border           glow state to set: "think" | "exploit" | "success"
     showApproval     reveal approve/reject controls
     hideApproval     hide approve/reject controls
     text(msg)        optional fn(msg) → string for log body; default uses payload summary
   ──────────────────────────────────────────────────────────────────────── */
const EVENT_MAP = {
  "engagement.created":             { label: "INIT",     color: "var(--accent)",  border: "think" },
  "engagement.run.created":         { label: "RUN",      color: "var(--accent)" },
  "phase.start":                    { label: "PHASE ▶",  color: "var(--accent)",  border: "think",
                                      text: m => `Starting phase: ${m.payload.phase || m.phase}` },
  "phase.end":                      { label: "PHASE ■",  color: "var(--accent)",
                                      text: m => `Finished phase: ${m.payload.phase || ""}  ${_phaseEndSummary(m)}` },
  "tool.invoked":                   { label: "TOOL ✓",   color: "var(--good)",
                                      okFalseLabel: "TOOL ✗", okFalseColor: "var(--bad)",
                                      text: m => _toolText(m) },
  "tool.rejected":                  { label: "TOOL ✗",   color: "var(--bad)",
                                      text: m => `${m.payload.tool || "?"} → ${m.payload.reason || "rejected"}` },
  "budget.warn_threshold":          { label: "BUDGET",   color: "var(--warn)",
                                      text: m => `Budget ${Math.round((m.budget.spent/m.budget.limit)*100)}% used ($${_f(m.budget.spent)} / $${_f(m.budget.limit)})` },
  "budget.pause_threshold":         { label: "BUDGET ⚠", color: "var(--warn)",
                                      text: m => `Budget near cap — ${Math.round((m.budget.spent/m.budget.limit)*100)}% used` },
  "budget.exhausted":               { label: "BUDGET !",  color: "var(--bad)",
                                      text: m => `Budget exhausted: $${_f(m.budget.spent)} / $${_f(m.budget.limit)}` },
  "engagement.paused_for_approval": { label: "APPROVAL", color: "var(--warn)",    showApproval: true, border: "exploit",
                                      text: m => `Paused — ${m.payload.suspected_findings || 0} suspected findings await review` },
  "engagement.approval.updated":    { label: "APPROVAL", color: "var(--accent)",  hideApproval: true,
                                      text: m => m.payload.approved ? "Approved — continuing to ConfirmEvidence" : "Rejected — engagement closed" },
  "tier4.navigation.result":        { label: "AI-NAV",   color: "var(--goal)",
                                      text: m => m.payload.ok ? "AI navigation succeeded" : `AI nav failed: ${(m.payload.error||"").slice(0,80)}` },
  "engagement.completed":           { label: "DONE ✓",   color: "var(--good)",    border: "success",
                                      text: m => `Complete — ${m.payload.confirmed || 0} confirmed finding(s)` },
  "engagement.failed":              { label: "FAIL",     color: "var(--bad)",     border: "exploit",
                                      text: m => `Failed: ${(m.payload.error || "unknown").slice(0,120)}` },
  "_default":                       { label: "EVENT",    color: "var(--muted)" },
};

/* ── Helpers ── */
function _f(n) { return (n || 0).toFixed(3); }

function _phaseEndSummary(m) {
  const p = m.payload || {};
  const parts = [];
  if (p.endpoints != null) parts.push(`${p.endpoints} endpoints`);
  if (p.suspected  != null) parts.push(`${p.suspected} suspected`);
  if (p.confirmed  != null) parts.push(`${p.confirmed} confirmed`);
  return parts.join(", ");
}

function _toolText(m) {
  const p = m.payload || {};
  const ok = p.ok !== false;
  const dur = p.duration_ms != null ? `${Math.round(p.duration_ms)}ms` : "—";
  const cost = p.cost_usd != null ? `$${_f(p.cost_usd)}` : "";
  const status = ok ? "ok" : `failed: ${(p.error || "").slice(0, 60)}`;
  return `${p.tool || "?"} → ${p.target || ""} [${status}] (${dur}${cost ? ", " + cost : ""})`;
}

function _esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function _now() {
  return new Date().toTimeString().slice(0, 8);
}

/* ── State ── */
let engagementId = null;
let es = null;  // EventSource
let lastStatus = null;
const toolLog = [];

/* ── DOM refs ── */
const $ = id => document.getElementById(id);
const log        = () => $("log");
const nowPhaseEl = () => $("nowPhase");
const nowTextEl  = () => $("nowText");
const glowCol    = () => $("glowCol");
const engLabel   = () => $("engIdLabel");

/* ── Glow border ───────────────────────────────────────────────────────────*/
const GLOW_CLASSES = ["glow-think", "glow-exploit", "glow-success"];

function updateGlowBorder(state) {
  const el = glowCol();
  if (!el) return;
  GLOW_CLASSES.forEach(c => el.classList.remove(c));
  if (state === "think")   el.classList.add("glow-think");
  if (state === "exploit") el.classList.add("glow-exploit");
  if (state === "success") el.classList.add("glow-success");
}

/* ── Log row ───────────────────────────────────────────────────────────────*/
function appendLogRow(time, label, color, text, extraLines) {
  const logEl = log();
  if (!logEl) return;

  // Remove placeholder on first real row
  const ph = logEl.querySelector(".log-placeholder");
  if (ph) ph.remove();

  const entry = document.createElement("div");
  entry.className = "log-entry";
  entry.style.borderLeftColor = color;

  const meta = document.createElement("div");
  meta.className = "log-entry-meta";

  const chip = document.createElement("span");
  chip.className = "chip";
  chip.style.color = color;
  chip.textContent = label;

  const timeEl = document.createElement("span");
  timeEl.className = "log-time";
  timeEl.textContent = time;

  meta.appendChild(chip);
  meta.appendChild(timeEl);
  entry.appendChild(meta);

  if (text) {
    const textEl = document.createElement("div");
    textEl.className = "log-text";
    textEl.textContent = text;
    entry.appendChild(textEl);
  }

  if (extraLines) {
    const extra = document.createElement("div");
    extra.className = "log-extra";
    extra.textContent = extraLines;
    entry.appendChild(extra);
  }

  logEl.appendChild(entry);
  logEl.scrollTop = logEl.scrollHeight;
}

/* ── Now bar ───────────────────────────────────────────────────────────────*/
function updateNowBar(phase, text) {
  const phEl = nowPhaseEl();
  const txEl = nowTextEl();
  if (phEl && phase) phEl.textContent = `Phase: ${phase}`;
  if (txEl && text)  txEl.textContent = text.slice(0, 160);
}

/* ── Side panels ───────────────────────────────────────────────────────────*/
function renderStatus(msg) {
  const el = $("statusBody");
  if (!el) return;
  const s = msg.status || "—";
  const cls = s === "completed" ? "clr-good" : s.includes("paused") ? "clr-warn" : s.includes("failed") || s === "budget_exhausted" ? "clr-bad" : "";
  const budgetPct = msg.budget && msg.budget.limit > 0
    ? Math.round((msg.budget.spent / msg.budget.limit) * 100) : 0;
  el.innerHTML = `
    <div class="metric"><span>Status</span><span class="metric-val ${cls}">${_esc(s)}</span></div>
    <div class="metric"><span>Phase</span><span class="metric-val">${_esc(msg.phase || "—")}</span></div>
    <div class="metric"><span>Budget</span><span class="metric-val">$${_f(msg.budget && msg.budget.spent)} / $${_f(msg.budget && msg.budget.limit)} (${budgetPct}%)</span></div>
    <div class="metric"><span>Tool calls</span><span class="metric-val">${toolLog.length}</span></div>
    <div class="metric"><span>Tool budget</span><span class="metric-val">$${_f(msg.budget && msg.budget.tool_spent)} / $${_f(msg.budget && (msg.budget.tool_cap || 5.0))}</span></div>
  `;
}

function renderToolActivity(msg) {
  const el = $("toolBody");
  if (!el) return;
  const p = msg.payload || {};
  if (msg.type === "tool.invoked" || msg.type === "tool.rejected") {
    const ok = p.ok !== false && msg.type !== "tool.rejected";
    toolLog.push({ tool: p.tool || "?", target: p.target || "", ok, dur: p.duration_ms, cost: p.cost_usd, err: p.error || p.reason || "" });
  }
  if (toolLog.length === 0) {
    el.innerHTML = '<div class="clr-muted" style="font-size:11px;padding:4px 0">No tool activity yet.</div>';
    return;
  }
  el.innerHTML = toolLog.slice(-20).map(t => {
    const statusCls = t.ok ? "tool-ok" : "tool-fail";
    const statusText = t.ok ? "ok" : _esc(t.err.slice(0, 50));
    const dur = t.dur != null ? `${Math.round(t.dur)}ms` : "";
    const cost = t.cost != null ? `$${_f(t.cost)}` : "";
    return `<div class="tool-item">
      <span class="tool-name">${_esc(t.tool)}</span> <span class="clr-muted">${_esc(t.target)}</span>
      → <span class="${statusCls}">${statusText}</span>
      <span class="tool-meta">${[dur, cost].filter(Boolean).join(", ")}</span>
    </div>`;
  }).join("");
}

function renderFindings(msg) {
  /* Findings are refreshed by a periodic poll of /engagements/{id}/findings
     so the panel stays current even from events that don't carry finding data. */
  if (!engagementId) return;
  fetch(`/engagements/${engagementId}/findings`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data) return;
      const el = $("findingsBody");
      if (!el) return;
      const suspected  = data.suspected_findings  || [];
      const confirmed  = data.confirmed_findings  || [];
      let html = `<div class="metric"><span>Suspected</span><span class="metric-val clr-warn">${suspected.length}</span></div>
                  <div class="metric"><span>Confirmed</span><span class="metric-val clr-good">${confirmed.length}</span></div>`;
      confirmed.slice(0, 5).forEach(f => {
        const sevCls = `sev-${f.severity || "medium"}`;
        html += `<div class="finding-card">
          <h3 class="${sevCls}">${_esc(f.title || f.vuln_type)}</h3>
          <p>${_esc(f.endpoint || "")} · <strong>${_esc(f.severity || "medium")}</strong></p>
        </div>`;
      });
      suspected.slice(0, 3).forEach(f => {
        html += `<div class="finding-card" style="opacity:0.75">
          <h3 class="clr-warn">${_esc(f.title || f.vuln_type)}</h3>
          <p>${_esc(f.endpoint || "")} · suspected</p>
        </div>`;
      });
      el.innerHTML = html;
    })
    .catch(() => {});
}

function updateSidePanels(msg) {
  renderStatus(msg);
  renderToolActivity(msg);
  if (["phase.end", "engagement.completed", "engagement.paused_for_approval"].includes(msg.type)) {
    renderFindings(msg);
  }
}

/* ── Approval controls ─────────────────────────────────────────────────────*/
function showApprovalControls(show) {
  const a = $("approveBtn");
  const r = $("rejectBtn");
  if (a) a.style.display = show ? "inline-block" : "none";
  if (r) r.style.display = show ? "inline-block" : "none";
}

/* ── Report overlay ────────────────────────────────────────────────────────*/
async function showReportOverlay() {
  if (!engagementId) return;
  const resp = await fetch(`/engagements/${engagementId}/report`);
  if (!resp.ok) return;
  const data = await resp.json();
  const r = data.report;
  if (!r) return;

  const box = $("reportBox");
  box.innerHTML = `
    <button class="close-btn" onclick="closeReport()">✕</button>
    <h2>${_esc(r.title)}</h2>
    <p><strong>Target:</strong> ${_esc(r.target)}</p>
    <p>${_esc(r.summary)}</p>
    <p><strong>Risk Overview:</strong>
      <span class="sev-critical">critical: ${r.findings_overview.critical||0}</span>,
      <span class="sev-high">high: ${r.findings_overview.high||0}</span>,
      <span class="sev-medium">medium: ${r.findings_overview.medium||0}</span>,
      <span class="sev-low">low: ${r.findings_overview.low||0}</span>
    </p>
    <p><strong>Top Risks:</strong><br>${(r.key_risks||[]).map(x => "• " + _esc(x)).join("<br>")}</p>
    <p><strong>Recommendations:</strong><br>${(r.recommendations||[]).map(x => "• " + _esc(x)).join("<br>")}</p>
  `;
  $("reportOverlay").classList.add("visible");
}

function closeReport() {
  $("reportOverlay").classList.remove("visible");
}

/* ── Main event handler ────────────────────────────────────────────────────*/
function handleEvent(msg) {
  const type = msg.type || "_default";
  let def = EVENT_MAP[type];

  if (!def) {
    /* Check for subtypes like budget.* not in map */
    if (type.startsWith("budget.")) def = EVENT_MAP["budget.warn_threshold"];
    else def = EVENT_MAP["_default"];
  }

  /* Resolve ok-sensitive label/color for tool.invoked */
  let label = def.label;
  let color = def.color;
  if (def.okFalseLabel && msg.payload && msg.payload.ok === false) {
    label = def.okFalseLabel;
    color = def.okFalseColor || def.color;
  }

  /* Build body text */
  let text = "";
  if (def.text) {
    try { text = def.text(msg); } catch(_) { text = JSON.stringify(msg.payload || {}); }
  } else {
    const p = msg.payload || {};
    const keys = Object.keys(p).slice(0, 4);
    text = keys.map(k => `${k}: ${String(p[k]).slice(0, 60)}`).join("  ");
  }

  /* Timestamp from message or current time */
  const time = msg.ts ? msg.ts.slice(11, 19) : _now();

  appendLogRow(time, label, color, text);
  updateNowBar(msg.phase, text);
  updateSidePanels(msg);

  if (def.border) updateGlowBorder(def.border);
  if (def.showApproval) showApprovalControls(true);
  if (def.hideApproval) showApprovalControls(false);

  lastStatus = msg.status;

  /* On completion show report button */
  if (msg.status === "completed") {
    const btn = $("reportBtn");
    if (btn) btn.style.display = "inline-block";
  }
}

/* ── EventSource lifecycle ─────────────────────────────────────────────────*/
function openStream(eid) {
  if (es) { es.close(); es = null; }

  es = new EventSource(`/engagements/${eid}/stream`);

  es.onmessage = evt => {
    try { handleEvent(JSON.parse(evt.data)); } catch(e) { console.warn("parse error", e); }
  };

  es.onerror = () => {
    /* Connection lost — SSE auto-reconnects; show a note in log */
    if (lastStatus && ["completed", "failed", "budget_exhausted"].includes(lastStatus)) {
      es.close();
    }
  };
}

/* ── API helpers ───────────────────────────────────────────────────────────*/
async function createEngagement() {
  const target = ($("targetUrl").value || "").trim();
  if (!target) return alert("Enter a target URL");
  const mode   = $("approvalMode").value;
  const budget = parseFloat($("budgetUsd").value) || 50;

  const resp = await fetch("/engagements", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_url: target, approval_mode: mode, budget_usd: budget }),
  });
  if (!resp.ok) { alert("Create failed"); return; }
  const data = await resp.json();
  engagementId = data.engagement_id;
  engLabel().textContent = `engagement: ${engagementId}`;

  /* Reset UI */
  toolLog.length = 0;
  log().innerHTML = '<div class="log-placeholder">waiting for agent…</div>';
  showApprovalControls(false);
  updateGlowBorder("think");
  const btn = $("reportBtn");
  if (btn) btn.style.display = "none";

  appendLogRow(_now(), "INIT", "var(--accent)", `Created engagement for ${target}`);
}

async function startEngagement() {
  if (!engagementId) return alert("Create an engagement first");
  const resp = await fetch(`/engagements/${engagementId}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_steps_per_agent: 12, step_delay_ms: 150 }),
  });
  if (!resp.ok) { alert("Start failed"); return; }
  openStream(engagementId);
}

async function approveEngagement(approved) {
  if (!engagementId) return;
  const note = approved ? "approved from ops console" : "rejected from ops console";
  await fetch(`/engagements/${engagementId}/approval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved, note }),
  });
}

/* ── Init ──────────────────────────────────────────────────────────────────*/
document.addEventListener("DOMContentLoaded", () => {
  $("createBtn") .addEventListener("click", createEngagement);
  $("startBtn")  .addEventListener("click", startEngagement);
  $("approveBtn").addEventListener("click", () => approveEngagement(true));
  $("rejectBtn") .addEventListener("click", () => approveEngagement(false));
  $("reportBtn") .addEventListener("click", showReportOverlay);

  /* Pre-fill target from ?target= query param if present */
  const params = new URLSearchParams(window.location.search);
  const t = params.get("target");
  if (t) $("targetUrl").value = t;

  /* Fetch /health and update the Tools badge */
  fetch("/health")
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      const badge = $("toolsBadge");
      if (!badge || !data) return;
      const caps = data.capabilities || {};
      const on = caps.tool_channel_enabled && caps.hexstrike_reachable;
      badge.textContent = on ? "Tools: ON" : "Tools: OFF";
      badge.style.color = on ? "var(--good)" : "var(--muted)";
      badge.style.borderColor = on ? "var(--good)" : "var(--line)";
    })
    .catch(() => {});
});
