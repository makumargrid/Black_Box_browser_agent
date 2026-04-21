from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from blackbox_service.models import (
    ActionRequest,
    ActionResponse,
    AgentStartRequest,
    StartRunRequest,
    StartRunResponse,
)
from blackbox_service.service import BlackboxService, RunNotFoundError


def create_app(
    db_path: str | Path = "blackbox_events.db",
    use_playwright: bool = False,
    browser_headless: bool = False,
    planner=None,
    default_target_url: str = "http://127.0.0.1:3000/#/",
    default_agent_max_steps: int = 8,
    default_agent_step_delay_ms: int = 400,
) -> FastAPI:
    app = FastAPI(
        title="Blackbox Browser Agent",
        version="0.1.0",
        description="External webapp blackbox automation service with live thought/event streaming.",
    )
    app.state.service = BlackboxService(
        db_path=db_path,
        use_playwright=use_playwright,
        browser_headless=browser_headless,
        planner=planner,
    )
    app.state.default_target_url = default_target_url
    app.state.default_agent_max_steps = default_agent_max_steps
    app.state.default_agent_step_delay_ms = default_agent_step_delay_ms

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", response_model=StartRunResponse, status_code=201)
    def create_run(body: StartRunRequest) -> StartRunResponse:
        run = app.state.service.start_run(targets=body.targets, options=body.options)
        return StartRunResponse(
            run_id=run.run_id,
            status=run.status,
            targets=run.targets,
            active_tab_id=run.active_tab_id,
        )

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            run = app.state.service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc
        return run.model_dump(mode="json")

    @app.get("/runs/{run_id}/tabs")
    def list_tabs(run_id: str) -> dict[str, Any]:
        try:
            tabs = app.state.service.list_tabs(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc
        return {"run_id": run_id, "tabs": [tab.model_dump(mode="json") for tab in tabs]}

    @app.post("/runs/{run_id}/actions", response_model=ActionResponse)
    def execute_action(run_id: str, body: ActionRequest) -> ActionResponse:
        try:
            result = app.state.service.execute_action(
                run_id=run_id,
                action_type=body.action_type,
                params=body.params,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ActionResponse(ok=True, action_type=body.action_type, result=result["result"])

    @app.get("/runs/{run_id}/memory")
    def list_memory(run_id: str, limit: int = 500) -> dict[str, Any]:
        try:
            events = app.state.service.list_memory(run_id, limit=limit)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc
        return {"run_id": run_id, "events": [event.model_dump(mode="json") for event in events]}

    @app.get("/runs/{run_id}/artifacts")
    def list_artifacts(run_id: str) -> dict[str, Any]:
        try:
            return app.state.service.list_artifacts(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc

    @app.post("/runs/{run_id}/stop")
    def stop_run(run_id: str) -> dict[str, Any]:
        try:
            run = app.state.service.stop_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc
        return run.model_dump(mode="json")

    @app.post("/runs/{run_id}/agent/start", status_code=202)
    def start_agent(run_id: str, body: AgentStartRequest) -> dict[str, Any]:
        try:
            return app.state.service.start_agent(
                run_id=run_id,
                max_steps=body.max_steps,
                step_delay_ms=body.step_delay_ms,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc

    @app.get("/runs/{run_id}/agent/state")
    def get_agent_state(run_id: str) -> dict[str, Any]:
        try:
            return app.state.service.get_agent_state(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc

    @app.get("/runs/{run_id}/stream")
    async def stream_run(run_id: str):
        try:
            _ = app.state.service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}") from exc

        async def event_generator():
            async for event in app.state.service.stream_events(run_id):
                payload = json.dumps(event.model_dump(mode="json"))
                yield f"event: {event.type}\ndata: {payload}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/artifacts/{run_id}/{filename}")
    def serve_artifact(run_id: str, filename: str):
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", filename) or ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = Path("artifacts") / run_id / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(str(path), media_type="image/png")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        default_target = json.dumps(app.state.default_target_url)
        default_steps = int(app.state.default_agent_max_steps)
        default_delay = int(app.state.default_agent_step_delay_ms)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Blackbox Security Agent</title>
  <style>
    :root {{
      --bg: #0a0e17; --panel: #111827; --line: #1e2d42; --text: #d4e0f0;
      --muted: #5c7a9e; --accent: #3b9eff; --amber: #f59e0b; --red: #ef4444;
      --green: #22c55e; --purple: #a78bfa;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 13px;
      color: var(--text); background: var(--bg);
      background-image: radial-gradient(ellipse at 10% 0%, #0d1f33 0%, transparent 50%),
                        radial-gradient(ellipse at 90% 100%, #0e1c2e 0%, transparent 50%);
      height: 100vh; display: flex; flex-direction: column; overflow: hidden;
    }}
    /* ── Header bar ── */
    #header {{
      display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
      padding: 10px 16px; border-bottom: 1px solid var(--line);
      background: rgba(17,24,39,0.9); backdrop-filter: blur(8px);
      flex-shrink: 0;
    }}
    #header h1 {{ font-size: 14px; letter-spacing: 2px; text-transform: uppercase;
      color: var(--accent); white-space: nowrap; margin-right: 8px; }}
    #header input {{
      font-family: inherit; font-size: 12px; background: #0d1520; color: var(--text);
      border: 1px solid var(--line); border-radius: 5px; padding: 5px 8px;
    }}
    #targetUrl {{ width: 280px; }}
    #runId {{ width: 160px; }}
    #maxSteps, #stepDelay {{ width: 70px; }}
    button {{
      font-family: inherit; font-size: 12px; cursor: pointer; border-radius: 5px;
      padding: 5px 12px; border: 1px solid; white-space: nowrap;
    }}
    .btn-primary {{ background: #1a3a5c; border-color: #2d6aa0; color: var(--accent); }}
    .btn-primary:hover {{ background: #1f4570; }}
    .btn-danger {{ background: #3b1a1a; border-color: #7f2020; color: var(--red); }}
    .btn-danger:hover {{ background: #4a2020; }}
    .btn-ghost {{ background: transparent; border-color: var(--line); color: var(--muted); }}
    .btn-ghost:hover {{ border-color: var(--muted); color: var(--text); }}
    #statusBadge {{
      font-size: 11px; padding: 3px 10px; border-radius: 12px; border: 1px solid;
      border-color: var(--line); color: var(--muted); background: #0d1520;
      white-space: nowrap;
    }}
    #statusBadge.running {{ border-color: var(--amber); color: var(--amber); }}
    #statusBadge.completed {{ border-color: var(--green); color: var(--green); }}
    #statusBadge.failed {{ border-color: var(--red); color: var(--red); }}
    /* ── Progress bar ── */
    #progressWrap {{
      height: 3px; background: var(--line); flex-shrink: 0;
    }}
    #progressBar {{ height: 100%; width: 0%; background: var(--accent); transition: width 0.4s; }}
    /* ── Main layout ── */
    #main {{
      display: grid; grid-template-columns: 1fr 320px;
      gap: 0; flex: 1; overflow: hidden;
    }}
    #leftCol {{
      display: flex; flex-direction: column; border-right: 1px solid var(--line);
      overflow: hidden;
    }}
    /* ── Reasoning panel ── */
    #reasoningHeader {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 8px 14px; border-bottom: 1px solid var(--line);
      color: var(--muted); font-size: 11px; letter-spacing: 1px; text-transform: uppercase;
      flex-shrink: 0;
    }}
    #reasoningPanel {{
      flex: 1; overflow-y: auto; padding: 12px 14px;
    }}
    #reasoningPanel::-webkit-scrollbar {{ width: 4px; }}
    #reasoningPanel::-webkit-scrollbar-thumb {{ background: var(--line); border-radius: 2px; }}
    /* ── Reasoning cards ── */
    .rcard {{
      border-left: 3px solid var(--accent); margin-bottom: 14px;
      padding: 10px 14px; background: var(--panel);
      border-radius: 0 8px 8px 0; animation: fadeIn 0.3s ease;
    }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    .rcard.interact {{ border-left-color: var(--amber); }}
    .rcard.probe   {{ border-left-color: var(--red); }}
    .rcard.done    {{ border-left-color: var(--green); }}
    .rcard-top {{
      display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
    }}
    .step-badge {{
      font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
      color: var(--muted); white-space: nowrap;
    }}
    .action-pill {{
      font-size: 11px; padding: 2px 8px; border-radius: 4px;
      background: #0d1a2e; color: var(--accent); border: 1px solid #1e3554;
    }}
    .rcard.interact .action-pill {{ color: var(--amber); border-color: #5c3a00; background: #1e1200; }}
    .rcard.probe   .action-pill {{ color: var(--red);   border-color: #5c1010; background: #1e0808; }}
    .rcard.done    .action-pill {{ color: var(--green); border-color: #0e4020; background: #071a10; }}
    .rcard-thought {{
      font-size: 13px; line-height: 1.65; color: var(--text); margin-bottom: 6px;
    }}
    .rcard-hypothesis {{
      font-size: 12px; color: var(--muted); font-style: italic; margin-bottom: 6px;
      padding-left: 8px; border-left: 2px solid var(--line);
    }}
    .rcard-result {{
      font-size: 11px; color: #4ade80; padding: 4px 8px;
      background: rgba(34,197,94,0.06); border-radius: 4px; min-height: 22px;
    }}
    .rcard-result.waiting {{ color: var(--muted); }}
    /* ── Event log strip ── */
    #eventStrip {{
      height: 120px; border-top: 1px solid var(--line); overflow-y: auto;
      padding: 6px 14px; flex-shrink: 0;
    }}
    #eventStrip::-webkit-scrollbar {{ width: 4px; }}
    #eventStrip::-webkit-scrollbar-thumb {{ background: var(--line); }}
    .evline {{ font-size: 11px; line-height: 1.7; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .evline .evtype {{ padding: 0 5px; border-radius: 3px; font-size: 10px; margin-right: 5px; }}
    .evline .evtype.agent   {{ background: #0d2040; color: var(--accent); }}
    .evline .evtype.action  {{ background: #1e1200; color: var(--amber); }}
    .evline .evtype.observe {{ background: #071a10; color: var(--green); }}
    .evline .evtype.run     {{ background: #1a0d2e; color: var(--purple); }}
    /* ── Right sidebar ── */
    #rightCol {{
      display: flex; flex-direction: column; overflow: hidden;
    }}
    .sidebar-section {{
      border-bottom: 1px solid var(--line); padding: 10px 12px;
    }}
    .sidebar-label {{
      font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
      color: var(--muted); margin-bottom: 6px;
    }}
    #tabsList {{
      font-size: 11px; line-height: 2; color: var(--text);
      max-height: 100px; overflow-y: auto;
    }}
    #screenshotWrap {{ text-align: center; padding: 8px 0; }}
    #screenshotImg {{
      max-width: 100%; border-radius: 4px; border: 1px solid var(--line);
      display: none;
    }}
    #screenshotPlaceholder {{ color: var(--muted); font-size: 11px; padding: 20px 0; }}
    /* ── Manual action ── */
    #manualForm {{ display: flex; flex-direction: column; gap: 6px; }}
    select, textarea {{
      font-family: inherit; font-size: 12px; background: #0d1520; color: var(--text);
      border: 1px solid var(--line); border-radius: 5px; padding: 5px 8px;
      width: 100%;
    }}
    #actionParams {{ min-height: 60px; resize: vertical; }}
    @media (max-width: 800px) {{
      #main {{ grid-template-columns: 1fr; }}
      #rightCol {{ display: none; }}
    }}
  </style>
</head>
<body>
  <!-- Header -->
  <div id="header">
    <h1>&#9632; Blackbox</h1>
    <input id="targetUrl" placeholder="https://target.tld" title="Target URL"/>
    <button class="btn-primary" id="startRunBtn">&#9654; Start Run</button>
    <input id="runId" placeholder="run-..." title="Run ID"/>
    <button class="btn-ghost" id="connectBtn">Connect</button>
    <input id="maxSteps" type="number" min="1" value="{default_steps}" title="Max Steps"/>
    <span style="color:var(--muted);font-size:11px">steps</span>
    <input id="stepDelay" type="number" min="0" value="{default_delay}" title="Step Delay ms"/>
    <span style="color:var(--muted);font-size:11px">ms</span>
    <button class="btn-primary" id="startAgentBtn">&#9654; Start Agent</button>
    <button class="btn-ghost" id="pauseScrollBtn" title="Toggle auto-scroll">&#8613; Scroll</button>
    <span id="statusBadge">&#9679; idle</span>
  </div>
  <div id="progressWrap"><div id="progressBar"></div></div>

  <!-- Main -->
  <div id="main">
    <!-- Left: reasoning + event log -->
    <div id="leftCol">
      <div id="reasoningHeader">
        <span>Agent Reasoning</span>
        <span id="stepCounter" style="color:var(--text)">&#8212;</span>
      </div>
      <div id="reasoningPanel"></div>
      <div id="eventStrip"></div>
    </div>

    <!-- Right: sidebar -->
    <div id="rightCol">
      <div class="sidebar-section">
        <div class="sidebar-label">Browser Tabs</div>
        <div id="tabsList">&#8212;</div>
      </div>
      <div class="sidebar-section" style="flex:1;overflow:hidden;">
        <div class="sidebar-label">Latest Screenshot</div>
        <div id="screenshotWrap">
          <div id="screenshotPlaceholder">no screenshot yet</div>
          <img id="screenshotImg" alt="latest screenshot"/>
        </div>
      </div>
      <div class="sidebar-section">
        <div class="sidebar-label">Manual Action</div>
        <div id="manualForm">
          <select id="actionType">
            <option>get_page_content</option>
            <option>click</option>
            <option>fill</option>
            <option>navigate</option>
            <option>snapshot</option>
            <option>eval_js</option>
            <option>read_console</option>
            <option>read_network</option>
            <option>open_tab</option>
            <option>inject_html</option>
            <option>wait_for_selector</option>
            <option>select_option</option>
          </select>
          <textarea id="actionParams">{{}}</textarea>
          <button class="btn-ghost" id="executeActionBtn">Execute</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    const DEFAULT_TARGET = {default_target};
    let stream = null;
    let scrollPaused = false;
    let currentRunId = null;
    let currentMaxSteps = {default_steps};

    const OBSERVE_ACTIONS = new Set(["get_page_content","read_console","read_network","eval_js","snapshot","read_network"]);
    const INTERACT_ACTIONS = new Set(["click","fill","navigate","open_tab","select_option","wait_for_selector"]);
    const PROBE_ACTIONS = new Set(["inject_html"]);

    function cardClass(actionType) {{
      if (INTERACT_ACTIONS.has(actionType)) return "interact";
      if (PROBE_ACTIONS.has(actionType)) return "probe";
      if (actionType === "none") return "done";
      return "";
    }}

    function escHtml(s) {{
      return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }}

    function eventTypeClass(t) {{
      if (t.startsWith("agent")) return "agent";
      if (t.startsWith("action")) return "action";
      if (t.startsWith("observation")) return "observe";
      return "run";
    }}

    function addEventLine(type, brief) {{
      const strip = document.getElementById("eventStrip");
      const d = document.createElement("div");
      d.className = "evline";
      d.innerHTML = `<span class="evtype ${{eventTypeClass(type)}}">${{type}}</span>${{escHtml(brief)}}`;
      strip.appendChild(d);
      strip.scrollTop = strip.scrollHeight;
    }}

    function setStatus(text, cls) {{
      const b = document.getElementById("statusBadge");
      b.textContent = "● " + text;
      b.className = cls || "";
    }}

    function updateProgress(done, total) {{
      const pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
      document.getElementById("progressBar").style.width = pct + "%";
      document.getElementById("stepCounter").textContent = total > 0 ? `Step ${{done}} / ${{total}}` : "—";
    }}

    const observedEvents = [
      "run.started","run.stopped","action.open_tab","action.switch_tab","action.navigate",
      "action.eval_js","action.inject_html","observation.console","observation.network",
      "observation.page_content","artifact.screenshot","agent.started","agent.thought",
      "agent.hypothesis","agent.reasoning","agent.step.completed","agent.finished","agent.failed",
      "action.click","action.fill","action.select_option","action.wait_for_selector",
    ];

    function connectStream() {{
      const runId = (document.getElementById("runId").value || "").trim();
      if (!runId) return;
      currentRunId = runId;
      if (stream) stream.close();
      stream = new EventSource(`/runs/${{runId}}/stream`);

      stream.addEventListener("agent.reasoning", (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const p = d.payload || {{}};
          const cls = cardClass(p.action_type || "");
          const card = document.createElement("div");
          card.className = "rcard " + cls;
          card.id = `rcard-${{p.step_index}}`;
          card.innerHTML = `
            <div class="rcard-top">
              <span class="step-badge">Step ${{(p.step_index||0)+1}}</span>
              <span class="action-pill">${{escHtml(p.action_type||"?")}}</span>
            </div>
            <div class="rcard-thought">${{escHtml(p.thought||"")}}</div>
            <div class="rcard-hypothesis">${{escHtml(p.hypothesis||"")}}</div>
            <div class="rcard-result waiting" id="result-${{p.step_index}}">executing...</div>
          `;
          document.getElementById("reasoningPanel").appendChild(card);
          if (!scrollPaused) card.scrollIntoView({{behavior:"smooth",block:"end"}});
        }} catch(_) {{}}
      }});

      stream.addEventListener("agent.step.completed", (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const p = d.payload || {{}};
          const el = document.getElementById(`result-${{p.step_index}}`);
          if (el) {{
            el.className = "rcard-result";
            el.textContent = p.result_preview || "(done)";
          }}
          updateProgress(p.step_index + 1, currentMaxSteps);
        }} catch(_) {{}}
      }});

      stream.addEventListener("artifact.screenshot", (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const path = d.payload?.path || "";
          const parts = path.split("/");
          const filename = parts[parts.length - 1];
          if (filename && currentRunId) {{
            const img = document.getElementById("screenshotImg");
            img.src = `/artifacts/${{currentRunId}}/${{filename}}?t=${{Date.now()}}`;
            img.style.display = "block";
            document.getElementById("screenshotPlaceholder").style.display = "none";
          }}
        }} catch(_) {{}}
        addEventLine("artifact.screenshot", " screenshot captured");
      }});

      stream.addEventListener("agent.started", (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          currentMaxSteps = d.payload?.max_steps || {default_steps};
        }} catch(_) {{}}
        setStatus("running", "running");
        addEventLine("agent.started", " agent loop started");
      }});

      stream.addEventListener("agent.finished", (evt) => {{
        setStatus("completed", "completed");
        updateProgress(currentMaxSteps, currentMaxSteps);
        addEventLine("agent.finished", " agent finished");
      }});

      stream.addEventListener("agent.failed", (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          setStatus("failed: " + (d.payload?.error || "").slice(0,40), "failed");
          addEventLine("agent.failed", " " + (d.payload?.error || ""));
        }} catch(_) {{
          setStatus("failed", "failed");
        }}
      }});

      for (const evName of observedEvents) {{
        if (["agent.reasoning","agent.step.completed","artifact.screenshot","agent.started","agent.finished","agent.failed"].includes(evName)) continue;
        stream.addEventListener(evName, (evt) => {{
          try {{
            const d = JSON.parse(evt.data);
            const brief = d.payload ? JSON.stringify(d.payload).slice(0, 80) : "";
            addEventLine(evName, " " + brief);
          }} catch(_) {{
            addEventLine(evName, "");
          }}
        }});
      }}

      stream.onerror = () => addEventLine("stream", " [disconnected]");
    }}

    async function startRun() {{
      const target = (document.getElementById("targetUrl").value || "").trim();
      if (!target) {{ addEventLine("error", " target URL is required"); return; }}
      const resp = await fetch("/runs", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{targets:[target], options:{{mode:"live"}}}}),
      }});
      if (!resp.ok) {{ addEventLine("error", " failed to create run"); return; }}
      const data = await resp.json();
      document.getElementById("runId").value = data.run_id;
      connectStream();
      refreshTabs();
    }}

    async function startAgent() {{
      const runId = (document.getElementById("runId").value || "").trim();
      if (!runId) return;
      currentMaxSteps = Number(document.getElementById("maxSteps").value) || {default_steps};
      const resp = await fetch(`/runs/${{runId}}/agent/start`, {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{
          max_steps: currentMaxSteps,
          step_delay_ms: Number(document.getElementById("stepDelay").value) || 0,
        }}),
      }});
      if (!resp.ok) {{ addEventLine("error", " failed to start agent"); return; }}
      setStatus("running", "running");
    }}

    async function pollAgentState() {{
      const runId = (document.getElementById("runId").value || "").trim();
      if (!runId) return;
      const resp = await fetch(`/runs/${{runId}}/agent/state`);
      if (!resp.ok) return;
      const d = await resp.json();
      if (d.status !== "idle") {{
        setStatus(`${{d.status}} ${{d.steps_completed}}/${{d.max_steps}}`, d.status === "running" ? "running" : d.status === "completed" ? "completed" : "failed");
        updateProgress(d.steps_completed, d.max_steps);
      }}
    }}

    async function refreshTabs() {{
      const runId = (document.getElementById("runId").value || "").trim();
      if (!runId) return;
      const resp = await fetch(`/runs/${{runId}}/tabs`);
      if (!resp.ok) return;
      const d = await resp.json();
      document.getElementById("tabsList").innerHTML = d.tabs.map(t =>
        `<div>${{t.is_active?"▶":"&nbsp;&nbsp;"}} ${{escHtml(t.url.slice(0,45))}}</div>`
      ).join("") || "—";
    }}

    async function executeAction() {{
      const runId = (document.getElementById("runId").value || "").trim();
      if (!runId) return;
      let params = {{}};
      try {{ params = JSON.parse(document.getElementById("actionParams").value || "{{}}"); }}
      catch (_) {{ addEventLine("error", " invalid JSON params"); return; }}
      const resp = await fetch(`/runs/${{runId}}/actions`, {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{action_type: document.getElementById("actionType").value, params}}),
      }});
      if (!resp.ok) {{ addEventLine("error", " action failed"); return; }}
      refreshTabs();
    }}

    function parseQuery() {{
      const q = new URLSearchParams(window.location.search);
      return {{target:q.get("target"), autorun:q.get("autorun")==="1", autostartAgent:q.get("autostart_agent")==="1"}};
    }}

    document.getElementById("startRunBtn").addEventListener("click", startRun);
    document.getElementById("connectBtn").addEventListener("click", connectStream);
    document.getElementById("startAgentBtn").addEventListener("click", startAgent);
    document.getElementById("executeActionBtn").addEventListener("click", executeAction);
    document.getElementById("pauseScrollBtn").addEventListener("click", () => {{
      scrollPaused = !scrollPaused;
      document.getElementById("pauseScrollBtn").textContent = scrollPaused ? "⏸ Paused" : "↓ Scroll";
    }});

    document.getElementById("targetUrl").value = DEFAULT_TARGET;
    const q = parseQuery();
    if (q.target) document.getElementById("targetUrl").value = q.target;
    if (q.autorun) {{
      startRun().then(() => {{ if (q.autostartAgent) startAgent(); }});
    }}
    setInterval(() => {{ refreshTabs(); pollAgentState(); }}, 1500);
  </script>
</body>
</html>
"""

    return app
