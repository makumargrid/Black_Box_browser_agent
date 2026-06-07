from __future__ import annotations

import asyncio
import json
import queue as _queue
from pathlib import Path
from typing import Any

import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from blackbox_service.models import (
    ActionRequest,
    ActionResponse,
    AgentStartRequest,
    StartRunRequest,
    StartRunResponse,
)
from blackbox_service.engagement_models import (
    ApprovalRequest,
    CreateEngagementRequest,
    StartEngagementRequest,
)
from blackbox_service.orchestrator import EngagementNotFoundError, EngagementOrchestrator
from blackbox_service.service import BlackboxService, RunNotFoundError


def create_app(
    db_path: str | Path = "blackbox_events.db",
    use_playwright: bool = False,
    browser_headless: bool = False,
    planner=None,
    artifacts_dir: str | Path = "artifacts",
    strict_playwright_runtime: bool = False,
    anthropic_api_key: str = "",
    anthropic_model: str = "claude-sonnet-4-6",
    gemini_api_key: str = "",
    gemini_model: str = "gemini-2.5-flash",
    tier4_headless: bool = True,
    default_target_url: str = "http://localhost:3000",
    default_agent_max_steps: int = 8,
    default_agent_step_delay_ms: int = 400,
    hexstrike_enabled: bool = False,
    hexstrike_url: str = "http://localhost:8888",
    hexstrike_timeout_s: float = 300.0,
    tool_budget_hard_cap_usd: float = 5.0,
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
        artifacts_dir=artifacts_dir,
        strict_playwright_runtime=strict_playwright_runtime,
    )
    app.state.orchestrator = EngagementOrchestrator(
        service=app.state.service,
        fail_fast_llm=True,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        tier4_headless=tier4_headless,
        hexstrike_enabled=hexstrike_enabled,
        hexstrike_url=hexstrike_url,
        hexstrike_timeout_s=hexstrike_timeout_s,
        tool_budget_hard_cap_usd=tool_budget_hard_cap_usd,
        artifacts_dir=artifacts_dir,
    )
    app.state.default_target_url = default_target_url
    app.state.default_agent_max_steps = default_agent_max_steps
    app.state.default_agent_step_delay_ms = default_agent_step_delay_ms
    app.state.anthropic_model = anthropic_model
    app.state.anthropic_api_key = anthropic_api_key
    app.state.gemini_api_key = gemini_api_key
    app.state.gemini_model = gemini_model

    # Mount static files (ops console assets) if the directory exists.
    # Mounted before route definitions so /static/* takes precedence.
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    @app.get("/ops-console", response_class=HTMLResponse, include_in_schema=False)
    def ops_console() -> HTMLResponse:
        """Phase-A Operations Console — cinematic SSE live view."""
        html_path = Path(__file__).parent / "static" / "ops_console.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard")

    @app.get("/health")
    def health() -> dict[str, Any]:
        runtime = app.state.service.get_runtime_info()
        caps = app.state.orchestrator.runtime_capabilities()
        return {"status": "ok", "runtime": runtime, "capabilities": caps}

    @app.get("/config/models")
    def get_models_config() -> dict[str, Any]:
        has_anthropic = bool(app.state.anthropic_api_key)
        has_gemini = bool(app.state.gemini_api_key)
        models = []
        models.append({"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "anthropic", "available": has_anthropic})
        models.append({"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "provider": "anthropic", "available": has_anthropic})
        models.append({"id": "claude-haiku-4", "label": "Claude Haiku 4", "provider": "anthropic", "available": has_anthropic})
        models.append({"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "gemini", "available": has_gemini})
        models.append({"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "gemini", "available": has_gemini})
        return {
            "default_model": app.state.anthropic_model if has_anthropic else (app.state.gemini_model if has_gemini else ""),
            "models": models,
        }

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
                model=body.model,
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

    @app.post("/engagements", status_code=201)
    def create_engagement(body: CreateEngagementRequest) -> dict[str, Any]:
        rec = app.state.orchestrator.create_engagement(body)
        return rec.model_dump(mode="json")

    @app.post("/engagements/{engagement_id}/start")
    def start_engagement(engagement_id: str, body: StartEngagementRequest) -> dict[str, Any]:
        try:
            rec = app.state.orchestrator.start_engagement(
                engagement_id=engagement_id,
                max_steps_per_agent=body.max_steps_per_agent,
                step_delay_ms=body.step_delay_ms,
            )
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return rec.model_dump(mode="json")

    @app.get("/engagements/{engagement_id}")
    def get_engagement(engagement_id: str) -> dict[str, Any]:
        try:
            rec = app.state.orchestrator.get_engagement(engagement_id)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return {
            **rec.model_dump(mode="json"),
            "runtime": app.state.service.get_runtime_info(),
            "capabilities": app.state.orchestrator.runtime_capabilities(),
        }

    @app.get("/engagements/{engagement_id}/events")
    def get_engagement_events(engagement_id: str) -> dict[str, Any]:
        try:
            events = app.state.orchestrator.list_events(engagement_id)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return {"engagement_id": engagement_id, "events": events}

    @app.post("/engagements/{engagement_id}/approval")
    def set_approval(engagement_id: str, body: ApprovalRequest) -> dict[str, Any]:
        try:
            rec = app.state.orchestrator.approve(engagement_id, body)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return rec.model_dump(mode="json")

    @app.get("/engagements/{engagement_id}/findings")
    def get_findings(engagement_id: str) -> dict[str, Any]:
        try:
            rec = app.state.orchestrator.get_engagement(engagement_id)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return {
            "engagement_id": engagement_id,
            "suspected_findings": rec.suspected_findings,
            "confirmed_findings": rec.confirmed_findings,
        }

    @app.get("/engagements/{engagement_id}/report")
    def get_report(engagement_id: str) -> dict[str, Any]:
        try:
            rec = app.state.orchestrator.get_engagement(engagement_id)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return {
            "engagement_id": engagement_id,
            "status": rec.status,
            "report": rec.report,
        }

    @app.get("/engagements/{engagement_id}/tool-invocations")
    def get_tool_invocations(engagement_id: str) -> dict[str, Any]:
        try:
            rec = app.state.orchestrator.get_engagement(engagement_id)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc
        return {
            "engagement_id": engagement_id,
            "tool_invocations": [ti.model_dump(mode="json") for ti in rec.tool_invocations],
        }

    _TERMINAL_STATUSES = frozenset({"completed", "failed", "budget_exhausted"})

    @app.get("/engagements/{engagement_id}/stream")
    async def stream_engagement(engagement_id: str):
        """SSE stream for a live engagement.

        Replays all historical events on connect, then streams new events as
        they are published by the orchestrator background thread. Ends cleanly
        when the engagement reaches a terminal status. Handles client
        disconnects by unsubscribing the consumer queue in a ``finally`` block.

        Each SSE ``data:`` line is a JSON object enriched with the current
        engagement snapshot::

            {
                "type": str,          # event type (phase.start, tool.invoked, …)
                "ts": str,            # ISO-8601 UTC timestamp
                "payload": dict,      # event-specific payload
                "phase": str,         # current engagement phase
                "status": str,        # current engagement status
                "budget": {
                    "spent": float,
                    "limit": float,
                }
            }
        """
        try:
            rec = app.state.orchestrator.get_engagement(engagement_id)
        except EngagementNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown engagement_id: {engagement_id}") from exc

        bus = app.state.orchestrator._bus

        async def generate():
            # 1. Replay all events already in the history so a late-joining
            #    browser sees everything that happened before it connected.
            for evt in list(rec.events):
                enriched = {
                    "type": evt.type,
                    "ts": evt.ts.isoformat(),
                    "payload": evt.payload,
                    "phase": rec.current_phase,
                    "status": rec.status,
                    "budget": {
                        "spent": rec.budget.spent_usd,
                        "limit": rec.budget.limit_usd,
                    },
                }
                yield f"data: {json.dumps(enriched)}\n\n"

            # If engagement is already terminal after replaying history, stop.
            if rec.status in _TERMINAL_STATUSES:
                return

            # 2. Subscribe for live events published by the orchestrator thread.
            q = bus.subscribe(engagement_id)
            try:
                while True:
                    try:
                        msg = q.get_nowait()
                        yield f"data: {json.dumps(msg)}\n\n"
                        if msg.get("status") in _TERMINAL_STATUSES:
                            break
                    except _queue.Empty:
                        await asyncio.sleep(0.05)
            finally:
                bus.unsubscribe(engagement_id, q)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/artifacts/{run_id}/{filename}")
    def serve_artifact(run_id: str, filename: str):
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", filename) or ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = Path("artifacts") / run_id / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(str(path), media_type="image/png")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> Response:
        default_target = json.dumps(app.state.default_target_url)
        default_steps = int(app.state.default_agent_max_steps)
        default_delay = int(app.state.default_agent_step_delay_ms)
        model_name = app.state.anthropic_model
        model_name_js = json.dumps(model_name)
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Blackbox Security Agent</title>
  <style>
    :root {{
      --bg:#0a0e17; --panel:#111827; --line:#1e2d42; --text:#d4e0f0;
      --muted:#5c7a9e; --accent:#3b9eff; --amber:#f59e0b; --red:#ef4444;
      --green:#22c55e; --purple:#a78bfa; --critical:#ff4d4d; --high:#f59e0b;
      --medium:#facc15; --low:#60a5fa;
    }}
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{
      font-family:"SF Mono",Menlo,Consolas,monospace; font-size:13px;
      color:var(--text); background:var(--bg);
      background-image:radial-gradient(ellipse at 10% 0%,#0d1f33 0%,transparent 50%),
                       radial-gradient(ellipse at 90% 100%,#0e1c2e 0%,transparent 50%);
      height:100vh; display:flex; flex-direction:column; overflow:hidden;
    }}
    /* ── Header ── */
    #header {{
      display:flex; align-items:center; gap:8px; flex-wrap:wrap;
      padding:8px 14px; border-bottom:1px solid var(--line);
      background:rgba(17,24,39,0.97); backdrop-filter:blur(8px); flex-shrink:0;
    }}
    #header h1 {{ font-size:13px; letter-spacing:2px; color:var(--accent); white-space:nowrap; }}
    #header input {{
      font-family:inherit; font-size:12px; background:#0d1520; color:var(--text);
      border:1px solid var(--line); border-radius:5px; padding:5px 8px;
    }}
    #targetUrl {{ width:280px; }}
    #maxSteps  {{ width:55px; }}
    button {{
      font-family:inherit; font-size:12px; cursor:pointer; border-radius:5px;
      padding:5px 14px; border:1px solid; white-space:nowrap;
    }}
    .btn-launch {{
      background:linear-gradient(135deg,#1a4a7a,#0f3a64); border-color:#3b9eff;
      color:#7dcfff; font-weight:bold; letter-spacing:0.5px; padding:6px 18px;
    }}
    .btn-launch:hover {{ background:linear-gradient(135deg,#205a90,#1a4a7a); }}
    .btn-launch:disabled {{ opacity:0.4; cursor:not-allowed; }}
    .btn-stop  {{ background:#3b1a1a; border-color:#7f2020; color:var(--red); display:none; }}
    .btn-stop:hover {{ background:#4a2020; }}
    .btn-ghost {{ background:transparent; border-color:var(--line); color:var(--muted); }}
    .btn-ghost:hover {{ border-color:var(--muted); color:var(--text); }}
    #modelBadge {{
      font-size:10px; padding:3px 8px; border-radius:4px;
      background:#0d1a2e; border:1px solid #1e3554; color:var(--accent);
      white-space:nowrap; display:none;
    }}
    #modelSelect {{
      font-size:11px; padding:3px 8px; border-radius:4px;
      background:#0d1a2e; border:1px solid #1e3554; color:var(--accent);
      cursor:pointer; outline:none; appearance:none;
      -webkit-appearance:none; -moz-appearance:none;
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%234a9eff'/%3E%3C/svg%3E");
      background-repeat:no-repeat; background-position:right 6px center;
      padding-right:20px;
    }}
    #modelSelect:hover {{ border-color:var(--accent); }}
    #modelSelect:focus {{ border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }}
    #modelSelect option {{ background:#0d1520; color:var(--text); }}
    #modelSelect option:disabled {{ color:#555; }}
    }}
    #runMeta {{ font-size:10px; color:var(--muted); white-space:nowrap; max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
    #statusBadge {{
      font-size:11px; padding:4px 12px; border-radius:12px; border:1px solid;
      border-color:var(--line); color:var(--muted); background:#0d1520; white-space:nowrap; margin-left:auto;
    }}
    #statusBadge.running  {{ border-color:var(--amber); color:var(--amber); animation:pulse 1.8s infinite; }}
    #statusBadge.completed {{ border-color:var(--green); color:var(--green); }}
    #statusBadge.failed   {{ border-color:var(--red); color:var(--red); }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.55}} }}
    /* ── Progress ── */
    #progressWrap {{ height:3px; background:var(--line); flex-shrink:0; }}
    #progressBar  {{ height:100%; width:0%; background:var(--accent); transition:width 0.4s; }}
    /* ── Layout ── */
    #main {{ display:grid; grid-template-columns:1fr 340px; flex:1; overflow:hidden; }}
    #leftCol {{ display:flex; flex-direction:column; border-right:1px solid var(--line); overflow:hidden; }}
    /* ── Reasoning ── */
    #reasoningHeader {{
      display:flex; align-items:center; justify-content:space-between;
      padding:7px 14px; border-bottom:1px solid var(--line);
      color:var(--muted); font-size:11px; letter-spacing:1px; text-transform:uppercase; flex-shrink:0;
    }}
    #reasoningPanel {{ flex:1; overflow-y:auto; padding:14px; }}
    #reasoningPanel::-webkit-scrollbar {{ width:4px; }}
    #reasoningPanel::-webkit-scrollbar-thumb {{ background:var(--line); border-radius:2px; }}
    /* Empty state */
    #emptyState {{
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      height:100%; color:var(--muted); text-align:center; gap:10px;
    }}
    #emptyState .logo {{ font-size:28px; letter-spacing:4px; color:#1e3a5c; }}
    #emptyState .hint {{ font-size:12px; line-height:1.8; max-width:380px; }}
    #emptyState .hint code {{ background:#0d1a2e; border:1px solid var(--line); padding:1px 6px; border-radius:4px; color:var(--accent); }}
    /* Error banner */
    #errorBanner {{
      display:none; background:#200a0a; border:1px solid #7f2020; border-radius:8px;
      margin-bottom:14px; padding:12px 16px; animation:fadeIn 0.3s ease;
    }}
    #errorBanner .err-title {{ color:var(--red); font-size:12px; font-weight:bold; margin-bottom:6px; }}
    #errorBanner .err-body  {{ font-size:11px; color:#f87171; line-height:1.7; word-break:break-all; }}
    #errorBanner .err-hint  {{ margin-top:8px; font-size:11px; color:#9c6060; font-style:italic; }}
    /* Reasoning cards */
    .rcard {{
      border-left:3px solid var(--accent); margin-bottom:12px;
      padding:10px 14px; background:var(--panel); border-radius:0 8px 8px 0; animation:fadeIn 0.3s ease;
    }}
    @keyframes fadeIn {{ from {{opacity:0;transform:translateY(6px)}} to {{opacity:1;transform:translateY(0)}} }}
    .rcard.interact {{ border-left-color:var(--amber); }}
    .rcard.probe    {{ border-left-color:var(--red); }}
    .rcard.done     {{ border-left-color:var(--green); }}
    .rcard-top {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
    .step-badge {{ font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); white-space:nowrap; }}
    .action-pill {{ font-size:11px; padding:2px 8px; border-radius:4px; background:#0d1a2e; color:var(--accent); border:1px solid #1e3554; }}
    .rcard.interact .action-pill {{ color:var(--amber); border-color:#5c3a00; background:#1e1200; }}
    .rcard.probe    .action-pill {{ color:var(--red);   border-color:#5c1010; background:#1e0808; }}
    .rcard.done     .action-pill {{ color:var(--green); border-color:#0e4020; background:#071a10; }}
    .rcard-thought {{ font-size:13px; line-height:1.6; color:var(--text); margin-bottom:5px; }}
    .rcard-hypo    {{ font-size:11px; color:#7ea8cc; font-style:italic; margin-bottom:5px; padding-left:8px; border-left:2px solid var(--line); }}
    .rcard-result  {{ font-size:11px; color:#4ade80; padding:4px 8px; background:rgba(34,197,94,0.06); border-radius:4px; min-height:20px; }}
    .rcard-result.waiting {{ color:var(--muted); }}
    /* ── Event strip ── */
    #eventStrip {{
      height:90px; border-top:1px solid var(--line); overflow-y:auto;
      padding:5px 14px; flex-shrink:0; background:#080c14;
    }}
    #eventStrip::-webkit-scrollbar {{ width:4px; }}
    #eventStrip::-webkit-scrollbar-thumb {{ background:var(--line); }}
    .evline {{ font-size:11px; line-height:1.7; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .evline .evtype {{ padding:0 5px; border-radius:3px; font-size:10px; margin-right:5px; }}
    .evline .evtype.agent  {{ background:#0d2040; color:var(--accent); }}
    .evline .evtype.action {{ background:#1e1200; color:var(--amber); }}
    .evline .evtype.observe{{ background:#071a10; color:var(--green); }}
    .evline .evtype.run    {{ background:#1a0d2e; color:var(--purple); }}
    .evline .evtype.error  {{ background:#3b0a0a; color:var(--red); }}
    /* ── Right sidebar ── */
    #rightCol {{ display:flex; flex-direction:column; overflow:hidden; }}
    .sb-section {{ border-bottom:1px solid var(--line); padding:9px 12px; flex-shrink:0; }}
    .sb-label {{ font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); margin-bottom:5px; }}
    /* Findings */
    #findingsSection {{ flex:1; overflow-y:auto; min-height:100px; }}
    #findingsList {{ padding:4px 0; }}
    .fcard {{
      margin-bottom:8px; padding:8px 10px; border-radius:6px;
      background:#0d1a2e; border-left:3px solid var(--line); animation:fadeIn 0.3s ease;
    }}
    .fcard.critical {{ border-left-color:var(--critical); }}
    .fcard.high     {{ border-left-color:var(--high); }}
    .fcard.medium   {{ border-left-color:var(--medium); }}
    .fcard.low      {{ border-left-color:var(--low); }}
    .fcard-top {{ display:flex; align-items:center; gap:6px; margin-bottom:4px; }}
    .sev-badge {{
      font-size:9px; padding:1px 6px; border-radius:3px; font-weight:bold; text-transform:uppercase;
    }}
    .sev-badge.critical {{ background:rgba(255,77,77,0.2); color:var(--critical); }}
    .sev-badge.high     {{ background:rgba(245,158,11,0.2); color:var(--high); }}
    .sev-badge.medium   {{ background:rgba(250,204,21,0.2); color:var(--medium); }}
    .sev-badge.low      {{ background:rgba(96,165,250,0.2); color:var(--low); }}
    .fcard-type {{ font-size:11px; color:var(--text); font-weight:bold; }}
    .fcard-cwe  {{ font-size:10px; color:var(--muted); }}
    .fcard-hypo {{ font-size:11px; color:#8aa8c4; margin-top:3px; line-height:1.5; }}
    #noFindings {{ font-size:11px; color:var(--muted); padding:8px 0; font-style:italic; }}
    /* Screenshot */
    #screenshotWrap {{ text-align:center; padding:6px 0; }}
    #screenshotImg {{ max-width:100%; border-radius:4px; border:1px solid var(--line); display:none; cursor:pointer; }}
    #screenshotPlaceholder {{ color:var(--muted); font-size:11px; padding:12px 0; }}
    /* Tab display */
    #tabsList {{ font-size:11px; color:var(--text); line-height:1.8; }}
    /* ── Report Overlay ── */
    #reportOverlay {{
      display:none; position:fixed; inset:0; background:rgba(0,0,0,0.85);
      z-index:1000; overflow:auto; padding:20px;
    }}
    #reportOverlay.visible {{ display:flex; align-items:flex-start; justify-content:center; }}
    #reportPanel {{
      background:#0d1520; border:1px solid #2a4a6a; border-radius:12px;
      width:100%; max-width:820px; padding:32px 36px; animation:slideUp 0.35s ease;
      margin:auto;
    }}
    @keyframes slideUp {{ from {{transform:translateY(40px);opacity:0}} to {{transform:translateY(0);opacity:1}} }}
    #reportPanel pre {{
      white-space:pre-wrap; word-break:break-word; font-family:inherit;
      font-size:12px; line-height:1.75; color:#c8daf0;
    }}
    #reportHeader {{
      display:flex; align-items:center; justify-content:space-between;
      margin-bottom:18px; padding-bottom:14px; border-bottom:1px solid #2a4a6a;
    }}
    #reportHeader span {{ color:var(--accent); font-size:14px; letter-spacing:2px; font-weight:bold; }}
    #reportActions {{
      display:flex; gap:10px; margin-top:24px; justify-content:center; flex-wrap:wrap;
    }}
    #reportActions button {{ padding:8px 20px; font-size:13px; }}
    .btn-report-open  {{ background:linear-gradient(135deg,#0d3320,#071a10); border-color:#22c55e; color:#4ade80; font-weight:bold; padding:6px 14px; }}
    .btn-report-open:hover {{ background:linear-gradient(135deg,#124020,#0d2818); }}
    .btn-report-copy  {{ background:#0d2040; border-color:#2d5a8a; color:var(--accent); }}
    .btn-report-print {{ background:#071a10; border-color:#0e4020; color:var(--green); }}
    .btn-report-close {{ background:transparent; border-color:var(--line); color:var(--muted); }}
    /* severity colors in report text */
    .r-critical {{ color:#ff6b6b; font-weight:bold; }}
    .r-high     {{ color:#f59e0b; font-weight:bold; }}
    .r-medium   {{ color:#facc15; font-weight:bold; }}
    .r-low      {{ color:#60a5fa; }}
    @media print {{
      #reportOverlay {{ display:block!important; position:static; background:white; padding:0; }}
      #reportPanel {{ border:none; max-width:100%; color:black!important; }}
      #reportPanel pre {{ color:black!important; }}
      #reportActions {{ display:none; }}
    }}
    @media (max-width:800px) {{
      #main {{ grid-template-columns:1fr; }}
      #rightCol {{ display:none; }}
    }}
  </style>
</head>
<body>
  <div id="header">
    <h1>&#9632; BLACKBOX</h1>
    <input id="targetUrl" placeholder="http://target-url" title="Target URL to scan"/>
    <span style="color:var(--muted);font-size:11px">steps:</span>
    <input id="maxSteps" type="number" min="1" value="{default_steps}" title="Max agent steps"/>
    <button class="btn-launch" id="launchBtn">&#9654;&nbsp; LAUNCH</button>
    <button class="btn-stop"   id="stopBtn">&#9632;&nbsp; Stop</button>
    <button class="btn-report-open" id="viewReportBtn" style="display:none">&#128203; View Report</button>
    <button class="btn-ghost"  id="pauseScrollBtn" title="Toggle auto-scroll">&#8595;</button>
    <select id="modelSelect" title="Select AI model">
      <option value="{model_name}">{model_name}</option>
    </select>
    <div id="runMeta"></div>
    <span id="statusBadge">&#9679; ready</span>
  </div>
  <div id="progressWrap"><div id="progressBar"></div></div>

  <div id="main">
    <!-- Left: reasoning -->
    <div id="leftCol">
      <div id="reasoningHeader">
        <span>Agent Reasoning</span>
        <span id="stepCounter" style="color:var(--text)">&#8212;</span>
      </div>
      <div id="reasoningPanel">
        <div id="emptyState">
          <div class="logo">BLACKBOX</div>
          <div class="hint">
            Enter a target URL and click <strong style="color:var(--accent)">LAUNCH</strong>.<br>
            The agent opens a real browser, crawls the app, and attempts to find vulnerabilities.<br><br>
            <strong>Flow:</strong> RECON &rarr; AUTH TESTING &rarr; API PROBING &rarr; IDOR &rarr; REPORT<br><br>
            Need a target? &nbsp;<code>docker run -d -p 3000:3000 bkimminich/juice-shop</code>
          </div>
        </div>
        <div id="errorBanner">
          <div class="err-title">&#9888; Agent Failed</div>
          <div class="err-body" id="errorBody"></div>
          <div class="err-hint" id="errorHint"></div>
        </div>
      </div>
      <div id="eventStrip"></div>
    </div>

    <!-- Right: findings + screenshot + tab -->
    <div id="rightCol">
      <div class="sb-section" id="findingsSection" style="flex:1;overflow-y:auto;min-height:120px;">
        <div class="sb-label">Live Findings &nbsp;<span id="findingCount" style="color:var(--red);font-weight:bold"></span></div>
        <div id="findingsList"><div id="noFindings">Scanning… findings appear here.</div></div>
      </div>
      <div class="sb-section" style="flex:0 0 auto;">
        <div class="sb-label">Latest Screenshot</div>
        <div id="screenshotWrap">
          <div id="screenshotPlaceholder">no screenshot yet</div>
          <img id="screenshotImg" alt="screenshot" title="Click to enlarge" onclick="window.open(this.src)"/>
        </div>
      </div>
      <div class="sb-section" style="flex:0 0 auto;">
        <div class="sb-label">Browser</div>
        <div id="tabsList">&#8212;</div>
      </div>
    </div>
  </div>

  <!-- Full pentest report overlay — click dark area or X to close -->
  <div id="reportOverlay" onclick="if(event.target===this)closeReport()">
    <div id="reportPanel" onclick="event.stopPropagation()">
      <div id="reportHeader">
        <span>&#9632; PENETRATION TEST REPORT</span>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn-report-open" id="newScanBtn" onclick="closeReport();setTimeout(()=>document.getElementById('launchBtn').focus(),100)">&#9654; New Scan</button>
          <button class="btn-report-close" id="closeReportTopBtn" onclick="closeReport()" title="Esc to close">&#10005; Close</button>
        </div>
      </div>
      <pre id="reportContent"></pre>
      <div id="reportActions">
        <button class="btn-report-copy"  id="copyReportBtn">&#128203; Copy</button>
        <button class="btn-report-print" id="printReportBtn">&#128424; Print</button>
        <button class="btn-report-close" id="closeReportBtn" onclick="closeReport()">&#10005; Close</button>
      </div>
    </div>
  </div>

  <script>
    // Show JS errors in the event log so issues are visible
    window.onerror = (msg, src, line) => {{
      try {{ addEventLine('error', ` JS error: ${{msg}} (${{line}})`); }} catch(_) {{}}
      return false;
    }};

    const DEFAULT_TARGET = {default_target};
    const DEFAULT_DELAY  = {default_delay};
    let MODEL_NAME       = {model_name_js};

    let stream = null, scrollPaused = false, currentRunId = null;
    let currentMaxSteps = {default_steps}, isRunning = false;
    let startTime = null, allSteps = [], allFindings = [], stepResults = {{}};
    let targetUrl = DEFAULT_TARGET;

    // ── Vulnerability detection ────────────────────────────────────────
    const VULN_PATTERNS = [
      {{ re:/sqli|sql inject|OR 1=1|1=1--|union select/i,           type:'SQL Injection',           cwe:'CWE-89',  sev:'critical', cvss:'9.8' }},
      {{ re:/xss|cross.site.script|onerror=alert|alert.1./i,        type:'Cross-Site Scripting (XSS)', cwe:'CWE-79',  sev:'high',     cvss:'7.2' }},
      {{ re:/idor|insecure direct object|enumerat.*id|changed.*id/i, type:'IDOR',                  cwe:'CWE-284', sev:'high',     cvss:'7.5' }},
      {{ re:/auth.bypass|bypass.*auth|authentication bypass|bypassed auth/i, type:'Authentication Bypass', cwe:'CWE-287', sev:'critical', cvss:'9.1' }},
      {{ re:/missing.auth|no auth.*api|unauthenticated.*api|api.*without.*auth/i, type:'Missing API Authentication', cwe:'CWE-306', sev:'high', cvss:'7.5' }},
      {{ re:/admin.*accessible|admin.*reachable|admin.*bypass|admin.*without/i, type:'Broken Access Control',   cwe:'CWE-285', sev:'high',     cvss:'8.1' }},
      {{ re:/jwt.*found|token.*localStorage|token.*exposed|credentials.*exposed/i, type:'Sensitive Data Exposure', cwe:'CWE-200', sev:'medium', cvss:'5.3' }},
      {{ re:/command.inject|rce|remote.code.exec/i,                 type:'Remote Code Execution',  cwe:'CWE-78',  sev:'critical', cvss:'10.0' }},
      {{ re:/path.travers|directory.travers|dotdot|[.][.][/]/i,       type:'Path Traversal',         cwe:'CWE-22',  sev:'high',     cvss:'7.5' }},
      {{ re:/ssrf|server.side.request/i,                             type:'SSRF',                   cwe:'CWE-918', sev:'high',     cvss:'8.6' }},
    ];

    // Patterns in the agent's hypothesis/thought that indicate it CONFIRMED a real finding
    // Must be specific exploitation-confirmed language, NOT planning language like "test for vulnerabilities"
    const CONFIRMED_SIGNALS = /confirmed.*(vuln|exploit|bypass)|successfully (exploited|bypassed|injected|logged in)|data (leaked|exposed|dumped)|gained (access|admin)|payload (executed|reflected)|is vulnerable|is exploitable|authentication bypassed|sql.*(error|syntax|dump)|xss.*(reflected|executed|fired)|idor confirmed/i;

    // Patterns that indicate the attack FAILED — these BLOCK finding creation
    const FAILURE_SIGNALS = /unlikely to (work|succeed)|not vulnerable|properly (validated|sanitized|escaped)|rejected|invalid (email|input|credentials|password)|hardened|robust auth|anti.automation|csp protect|rate.limit|captcha|blocked|access.control.working|defense|safe against|no.*(vuln|exploit|finding)|could not|did not (succeed|work)|false.positive|benign|expect.*rejection|expect.*generic|mature defenses/i;

    // Patterns in the result that indicate failure (the action didn't actually exploit anything)
    const RESULT_FAILURE = /timeout|Timeout \d+ms exceeded|invalid email|enter a valid|captcha|rate.limit|403|401|redirect.*login|err_blocked|access denied|not found|please enter|validation error|ERR_/i;

    // Patterns in the result that indicate successful exploitation
    const RESULT_SUCCESS = /password|secret|token|api.key|admin.panel|dashboard.data|user.data|SELECT.*FROM|INSERT.*INTO|stack.trace|internal.server.error.*sql|database.error|logged.in|welcome.*admin|root:|uid=\d+/i;

    // Actions that can NEVER produce a finding by themselves (recon-only actions)
    const RECON_ONLY_ACTIONS = new Set(['navigate', 'open_tab', 'get_page_content', 'read_console', 'read_network', 'snapshot', 'wait_for_selector']);

    const REMEDIATION = {{
      'SQL Injection':            'Use parameterized queries/prepared statements. Never concatenate user input into SQL queries. Add input validation and WAF rules.',
      'Cross-Site Scripting (XSS)': 'Encode all output with context-aware encoding. Implement Content-Security-Policy header. Sanitize and validate all input.',
      'IDOR':                     'Implement server-side authorization checks on every resource access. Use indirect/opaque references instead of direct numeric IDs.',
      'Authentication Bypass':    'Use parameterized queries. Add account lockout and rate-limiting. Implement MFA. Audit all authentication paths.',
      'Missing API Authentication': 'Apply authentication middleware to all API routes. Deny by default. Audit route permissions on every release.',
      'Broken Access Control':    'Implement role-based access control (RBAC). Enforce access rules server-side on every request. Log and alert on policy violations.',
      'Sensitive Data Exposure':  'Do not store sensitive tokens in localStorage. Use httpOnly, Secure cookies. Encrypt data at rest and in transit.',
      'Remote Code Execution':    'Disable dangerous functions. Sandbox user input. Use allow-lists for system commands. Apply principle of least privilege.',
      'Path Traversal':           'Validate and sanitize file paths server-side. Use allow-lists for accessible directories. Never expose raw filesystem paths.',
      'SSRF':                     'Validate and restrict outbound requests. Use an allow-list of permitted destinations. Block access to internal network ranges.',
    }};

    const IMPACT = {{
      'SQL Injection':            'Attacker can bypass authentication, extract the entire database, modify or delete data, and potentially achieve RCE via database features.',
      'Cross-Site Scripting (XSS)': 'Attacker can steal session cookies, perform actions on behalf of victims, capture credentials, and distribute malware.',
      'IDOR':                     'Attacker can access, modify, or delete other users\\' data by manipulating identifiers.',
      'Authentication Bypass':    'Attacker can log in as any user (including admin) without valid credentials, gaining full account access.',
      'Missing API Authentication': 'Sensitive API data is accessible without any credentials, exposing user PII, business data, and internal structures.',
      'Broken Access Control':    'Attacker can access administrative functions or other users\\' data without authorization.',
      'Sensitive Data Exposure':  'Authentication tokens or credentials stored insecurely can be exfiltrated by an attacker with XSS or physical access.',
      'Remote Code Execution':    'Attacker can execute arbitrary code on the server, leading to full system compromise.',
      'Path Traversal':           'Attacker can read arbitrary files on the server, including configuration files and credentials.',
      'SSRF':                     'Attacker can make the server issue requests to internal services, potentially bypassing firewalls.',
    }};

    // ── Utilities ────────────────────────────────────────────────────
    function esc(s) {{
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}
    function evTypeClass(t) {{
      if (t.startsWith('agent'))       return 'agent';
      if (t.startsWith('action'))      return 'action';
      if (t.startsWith('observation')) return 'observe';
      if (t === 'error')               return 'error';
      return 'run';
    }}
    function cardClass(a) {{
      const INTERACT = new Set(['click','fill','navigate','open_tab','select_option','wait_for_selector']);
      const PROBE    = new Set(['inject_html']);
      if (INTERACT.has(a)) return 'interact';
      if (PROBE.has(a))    return 'probe';
      if (a === 'none')    return 'done';
      return '';
    }}
    function addEventLine(type, brief) {{
      const strip = document.getElementById('eventStrip');
      const d = document.createElement('div');
      d.className = 'evline';
      d.innerHTML = `<span class="evtype ${{evTypeClass(type)}}">${{type}}</span>${{esc(brief)}}`;
      strip.appendChild(d);
      strip.scrollTop = strip.scrollHeight;
    }}
    function setStatus(text, cls) {{
      const b = document.getElementById('statusBadge');
      b.textContent = '● ' + text;
      b.className = cls || '';
    }}
    function updateProgress(done, total) {{
      const pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
      document.getElementById('progressBar').style.width = pct + '%';
      document.getElementById('stepCounter').textContent = total > 0 ? `STEP ${{done}} / ${{total}}` : '—';
    }}
    function setRunning(running) {{
      isRunning = running;
      document.getElementById('launchBtn').disabled = running;
      document.getElementById('launchBtn').style.display = running ? 'none' : '';
      document.getElementById('stopBtn').style.display = running ? '' : 'none';
    }}
    function hideEmptyState() {{
      document.getElementById('emptyState').style.display = 'none';
      document.getElementById('errorBanner').style.display = 'none';
    }}
    function showError(msg) {{
      document.getElementById('errorBody').textContent = msg;
      let hint = '';
      if (msg.includes('401') || msg.includes('API key') || msg.includes('api_key'))
        hint = 'Hint: Set ANTHROPIC_API_KEY correctly in .env (not "replace-me").';
      else if (msg.includes('400') || msg.includes('Bad Request'))
        hint = 'Hint: Model name may be invalid. Try BLACKBOX_AGENT_MODEL=claude-sonnet-4-6 in .env.';
      else if (msg.includes('playwright') || msg.includes('browser'))
        hint = 'Hint: Run: uv run playwright install chromium';
      document.getElementById('errorHint').textContent = hint;
      document.getElementById('errorBanner').style.display = 'block';
      document.getElementById('emptyState').style.display = 'none';
    }}

    // ── Finding detection ────────────────────────────────────────────
    function tryExtractFinding(payload, resultPreview) {{
      const thought = payload.thought || '';
      const hypothesis = payload.hypothesis || '';
      const text = thought + ' ' + hypothesis;
      const result = resultPreview || '';
      const actionType = payload.action_type || '';
      const actionOk = !result.includes("'ok': False") && !result.includes('"ok": false') && !result.includes("'ok':False");

      // Gate 0: Recon-only actions (navigate, snapshot, etc.) can NEVER produce findings
      // You cannot exploit anything by just loading a page
      if (RECON_ONLY_ACTIONS.has(actionType)) return;

      for (const vp of VULN_PATTERNS) {{
        if (!vp.re.test(text)) continue;
        if (allFindings.find(f => f.type === vp.type)) continue;

        // ─── Evidence gate: require positive signals, reject on negative ───

        // 1. If the agent's reasoning explicitly says "not vulnerable" / "unlikely" / "failed" → skip
        if (FAILURE_SIGNALS.test(text)) continue;

        // 2. If the action result shows clear failure (timeout, validation error, 403) → skip
        if (RESULT_FAILURE.test(result)) continue;

        // 3. If the action itself failed (ok: False) → skip
        if (!actionOk) continue;

        // 4. Require at least ONE positive signal: either agent confirms, or result shows exploitation evidence
        const agentConfirmed = CONFIRMED_SIGNALS.test(text);
        const resultShowsEvidence = RESULT_SUCCESS.test(result);

        if (!agentConfirmed && !resultShowsEvidence) continue;

        // ─── Passed all gates — this is a credible finding ───
        const finding = {{
          id: allFindings.length + 1,
          type: vp.type, cwe: vp.cwe, sev: vp.sev, cvss: vp.cvss,
          hypothesis: hypothesis.slice(0, 120),
          thought: thought.slice(0, 200),
          action_type: actionType,
          evidence: result.slice(0, 400),
          step: (payload.step_index || 0) + 1,
        }};
        allFindings.push(finding);
        renderFindingCard(finding);
      }}
    }}

    function renderFindingCard(f) {{
      const noF = document.getElementById('noFindings');
      if (noF) noF.remove();
      document.getElementById('findingCount').textContent = `(${{allFindings.length}})`;

      const card = document.createElement('div');
      card.className = `fcard ${{f.sev}}`;
      card.innerHTML = `
        <div class="fcard-top">
          <span class="sev-badge ${{f.sev}}">${{f.sev}}</span>
          <span class="fcard-type">${{esc(f.type)}}</span>
          <span class="fcard-cwe">${{esc(f.cwe)}}</span>
        </div>
        <div class="fcard-hypo">${{esc(f.hypothesis || f.thought)}}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:3px">step ${{f.step}} &middot; ${{esc(f.action_type)}}</div>
      `;
      document.getElementById('findingsList').appendChild(card);
    }}

    // ── Report generation ────────────────────────────────────────────
    function buildReport() {{
      const now = new Date();
      const durationSec = startTime ? Math.round((Date.now() - startTime) / 1000) : 0;
      const dur = durationSec > 60
        ? `${{Math.floor(durationSec/60)}}m ${{durationSec%60}}s`
        : `${{durationSec}}s`;

      const sevOrder = {{ critical:0, high:1, medium:2, low:3 }};
      const sorted = [...allFindings].sort((a,b) => (sevOrder[a.sev]||9) - (sevOrder[b.sev]||9));
      const topSev = sorted.length > 0 ? sorted[0].sev.toUpperCase() : 'NONE';
      const sevCounts = {{ critical:0, high:0, medium:0, low:0 }};
      sorted.forEach(f => {{ if (f.sev in sevCounts) sevCounts[f.sev]++; }});

      // Executive summary
      let execSummary = '';
      if (sorted.length === 0) {{
        execSummary = 'No exploitable vulnerabilities were confirmed during this automated assessment. The application may still contain security issues not detectable through automated blackbox testing alone. Manual review is recommended.';
      }} else {{
        const critNames = sorted.filter(f=>f.sev==='critical').map(f=>f.type);
        execSummary = `Automated blackbox assessment of ${{targetUrl}} identified ${{sorted.length}} vulnerability${{sorted.length>1?'ies':''}} `;
        execSummary += `(${{sevCounts.critical}} critical, ${{sevCounts.high}} high, ${{sevCounts.medium}} medium, ${{sevCounts.low}} low). `;
        if (critNames.length > 0) {{
          execSummary += `The most critical finding is ${{critNames[0]}}, which `;
          execSummary += (IMPACT[critNames[0]] || 'represents a severe security risk.').split('.')[0] + '. ';
        }}
        execSummary += 'Immediate remediation of critical and high findings is strongly recommended.';
      }}

      // Timeline — pick the most significant steps
      const sigActions = new Set(['fill','click','eval_js','navigate','read_network']);
      const timeline = allSteps
        .filter(s => sigActions.has(s.action_type) || allFindings.some(f => f.step === s.step_index + 1))
        .slice(0, 10)
        .map(s => `  Step ${{String(s.step_index+1).padStart(2,' ')}}: [${{s.action_type.padEnd(12)}}] ${{(s.hypothesis || s.thought || '').slice(0,90)}}`)
        .join('\\n');

      const divider = '─'.repeat(62);

      let report = `BLACKBOX AUTOMATED PENETRATION TEST REPORT
${{divider}}
Target   : ${{targetUrl}}
Date     : ${{now.toLocaleDateString('en-US',{{year:'numeric',month:'long',day:'numeric'}})}} ${{now.toLocaleTimeString()}}
Duration : ${{dur}} (${{currentMaxSteps}} steps)
AI Model : ${{MODEL_NAME}}
Scanner  : Blackbox Security Agent (autonomous)
Risk     : ${{topSev}}
${{divider}}

EXECUTIVE SUMMARY
${{execSummary}}

VULNERABILITY SUMMARY
${{divider}}
`;
      if (sorted.length === 0) {{
        report += '  No vulnerabilities detected.\\n\\n';
        // Add useful info about what was tested even with no findings
        const defenses = allSteps
          .filter(s => {{
            const h = (s.hypothesis || '').toLowerCase();
            const t = (s.thought || '').toLowerCase();
            const combined = h + ' ' + t;
            return combined.includes('anti-automation') || combined.includes('captcha') ||
                   combined.includes('rate limit') || combined.includes('csp') ||
                   combined.includes('blocked') || combined.includes('hardened') ||
                   combined.includes('properly validated') || combined.includes('not vulnerable') ||
                   combined.includes('rejected') || combined.includes('defense');
          }})
          .map(s => `  • ${{(s.hypothesis || s.thought || '').slice(0, 100)}}`)
          .filter((v, i, a) => a.indexOf(v) === i)
          .slice(0, 5);
        if (defenses.length > 0) {{
          report += `DEFENSES OBSERVED\n${{divider}}\n${{defenses.join('\\n')}}\n\\n`;
        }}
        report += `ASSESSMENT NOTES\n${{divider}}\n`;
        report += `  Total steps executed: ${{allSteps.length}}\n`;
        report += `  The target appears well-defended against automated blackbox testing.\n`;
        report += `  Manual penetration testing may uncover issues not detectable by automated scanning.\n`;
      }} else {{
        sorted.forEach((f,i) => {{
          const mark = f.sev === 'critical' ? '●●' : f.sev === 'high' ? '● ' : '○ ';
          report += `  ${{mark}} #${{i+1}} [${{f.sev.toUpperCase().padEnd(8)}}] ${{f.type}} (${{f.cwe}})  CVSS ${{f.cvss}}\n`;
        }});
      }}

      if (sorted.length > 0) {{
        report += `\n${{divider}}\nDETAILED FINDINGS\n${{divider}}\n`;
        sorted.forEach((f, i) => {{
          report += `
FINDING #${{i+1}}: ${{f.type}}
${{divider}}
Severity    : ${{f.sev.toUpperCase()}}
CWE         : ${{f.cwe}}
CVSS Score  : ${{f.cvss}} (${{f.sev.toUpperCase()}})
Detected at : Step ${{f.step}} via ${{f.action_type}}

DESCRIPTION
${{f.thought || f.hypothesis}}

PROOF OF CONCEPT
${{f.hypothesis}}
`;
          if (f.evidence) {{
            report += `\nEVIDENCE\n${{f.evidence.slice(0, 400)}}\n`;
          }}
          report += `
IMPACT
${{IMPACT[f.type] || 'This vulnerability may allow unauthorized access or data exposure.'}}

REMEDIATION
${{REMEDIATION[f.type] || 'Apply security best practices and conduct a manual code review.'}}
${{divider}}
`;
        }});
      }}

      if (timeline) {{
        report += `\nATTACK TIMELINE (key steps)\n${{divider}}\n${{timeline}}\n`;
      }}

      report += `\n${{divider}}
Generated by Blackbox Security Agent  |  ${{now.toISOString()}}
This report was produced by automated AI-driven testing. Manual validation recommended.
${{divider}}`;

      return report;
    }}

    function showReport() {{
      document.getElementById('reportContent').textContent = buildReport();
      document.getElementById('reportOverlay').classList.add('visible');
      // Scroll report panel to top
      document.getElementById('reportPanel').scrollTop = 0;
    }}
    function closeReport() {{
      document.getElementById('reportOverlay').classList.remove('visible');
    }}

    // ── SSE Stream ───────────────────────────────────────────────────
    function openStream(runId) {{
      currentRunId = runId;
      if (stream) stream.close();
      stream = new EventSource(`/runs/${{runId}}/stream`);

      // Collect pending evidence for each step
      const pendingEvidence = {{}};

      stream.addEventListener('agent.reasoning', (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const p = d.payload || {{}};
          allSteps.push(p);
          const cls = cardClass(p.action_type || '');
          const card = document.createElement('div');
          card.className = 'rcard ' + cls;
          card.id = `rcard-${{p.step_index}}`;
          card.innerHTML = `
            <div class="rcard-top">
              <span class="step-badge">Step ${{(p.step_index||0)+1}}</span>
              <span class="action-pill">${{esc(p.action_type||'?')}}</span>
            </div>
            <div class="rcard-thought">${{esc(p.thought||'')}}</div>
            <div class="rcard-hypo">${{esc(p.hypothesis||'')}}</div>
            <div class="rcard-result waiting" id="result-${{p.step_index}}">executing…</div>
          `;
          document.getElementById('reasoningPanel').appendChild(card);
          if (!scrollPaused) card.scrollIntoView({{behavior:'smooth',block:'end'}});
          pendingEvidence[p.step_index] = p;
        }} catch(_) {{}}
      }});

      stream.addEventListener('agent.step.completed', (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const p = d.payload || {{}};
          const el = document.getElementById(`result-${{p.step_index}}`);
          if (el) {{
            el.className = 'rcard-result';
            el.textContent = p.result_preview || '(done)';
          }}
          stepResults[p.step_index] = p.result_preview || '';
          // Now we have evidence — try to extract a finding
          const reasoning = pendingEvidence[p.step_index];
          if (reasoning) tryExtractFinding(reasoning, p.result_preview || '');
          updateProgress(p.step_index + 1, currentMaxSteps);
        }} catch(_) {{}}
      }});

      stream.addEventListener('artifact.screenshot', (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const path = d.payload?.path || '';
          const fname = path.split('/').pop();
          if (fname && currentRunId) {{
            const img = document.getElementById('screenshotImg');
            img.src = `/artifacts/${{currentRunId}}/${{fname}}?t=${{Date.now()}}`;
            img.style.display = 'block';
            document.getElementById('screenshotPlaceholder').style.display = 'none';
          }}
        }} catch(_) {{}}
        addEventLine('artifact.screenshot', ' screenshot captured');
      }});

      stream.addEventListener('agent.started', (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          currentMaxSteps = d.payload?.max_steps || currentMaxSteps;
        }} catch(_) {{}}
        startTime = Date.now();
        setStatus('scanning…', 'running');
        addEventLine('agent.started', ' agent loop started');
      }});

      stream.addEventListener('agent.finished', (evt) => {{
        const fc = allFindings.length;
        setStatus('done · ' + fc + ' finding' + (fc !== 1 ? 's' : ''), 'completed');
        setRunning(false);
        updateProgress(currentMaxSteps, currentMaxSteps);
        addEventLine('agent.finished', ` scan complete · ${{fc}} finding(s)`);
        // Show "View Report" button with pulse — user clicks when ready
        const vrb = document.getElementById('viewReportBtn');
        vrb.style.display = '';
        vrb.style.animation = 'pulse 0.8s 4';
        vrb.textContent = '&#128203; View Report (' + fc + ' finding' + (fc !== 1 ? 's' : '') + ')';
        vrb.innerHTML = '&#128203;&nbsp;View Report &nbsp;<span style="color:var(--critical)">' + fc + ' finding' + (fc !== 1 ? 's' : '') + '</span>';
      }});

      stream.addEventListener('agent.failed', (evt) => {{
        try {{
          const d = JSON.parse(evt.data);
          const errMsg = d.payload?.error || 'unknown error';
          setStatus('failed', 'failed');
          setRunning(false);
          addEventLine('agent.failed', ' ' + errMsg);
          showError(errMsg);
        }} catch(_) {{
          setStatus('failed', 'failed');
          setRunning(false);
        }}
      }});

      const otherEvents = [
        'run.started','run.stopped','action.open_tab','action.switch_tab','action.navigate',
        'action.eval_js','action.inject_html','observation.console','observation.network',
        'observation.page_content','action.click','action.fill','action.select_option','action.wait_for_selector',
      ];
      for (const evName of otherEvents) {{
        stream.addEventListener(evName, (evt) => {{
          try {{
            const d = JSON.parse(evt.data);
            const brief = d.payload ? JSON.stringify(d.payload).slice(0, 80) : '';
            addEventLine(evName, ' ' + brief);
          }} catch(_) {{ addEventLine(evName, ''); }}
        }});
      }}
      stream.onerror = () => addEventLine('stream', ' [disconnected]');
    }}

    // ── Launch / Stop ────────────────────────────────────────────────
    async function launch() {{
      // Diagnostic: confirm button fires — visible in event strip
      try {{ addEventLine('launch', ' LAUNCH clicked'); }} catch(_) {{}}
      console.log('[Blackbox] launch() called');

      targetUrl = ((document.getElementById('targetUrl') || {{}}).value || '').trim();
      if (!targetUrl) {{
        showError('Target URL is required (e.g. https://juice-shop.herokuapp.com).');
        return;
      }}

      // Immediate UI feedback — BEFORE any DOM cleanup that might throw
      setStatus('starting…', 'running');
      setRunning(true);

      // Reset display state (non-fatal — old cached pages may lack these elements)
      try {{
        allSteps = []; allFindings = []; stepResults = {{}};
        const fl = document.getElementById('findingsList');
        if (fl) fl.innerHTML = '<div id="noFindings">Scanning… findings appear here.</div>';
        const fc = document.getElementById('findingCount');
        if (fc) fc.textContent = '';
        const si = document.getElementById('screenshotImg');
        if (si) si.style.display = 'none';
        const sp = document.getElementById('screenshotPlaceholder');
        if (sp) sp.style.display = '';
        const rp = document.getElementById('reasoningPanel');
        if (rp) rp.querySelectorAll('.rcard').forEach(c => c.remove());
        const ro = document.getElementById('reportOverlay');
        if (ro) ro.classList.remove('visible');
        const vr = document.getElementById('viewReportBtn');
        if (vr) vr.style.display = 'none';
      }} catch(resetErr) {{
        addEventLine('error', ' Reset error: ' + resetErr.message);
        console.error('[Blackbox] Reset error:', resetErr);
      }}

      try {{ hideEmptyState(); }} catch(_) {{}}

      let runData;
      try {{
        const resp = await fetch('/runs', {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{targets:[targetUrl], options:{{mode:'live'}}}}),
        }});
        if (!resp.ok) throw new Error(`HTTP ${{resp.status}}: ${{await resp.text()}}`);
        runData = await resp.json();
      }} catch(e) {{
        setStatus('error','failed'); setRunning(false); showError('Failed to create run: ' + e.message);
        return;
      }}

      currentRunId = runData.run_id;
      document.getElementById('runMeta').textContent = runData.run_id;
      addEventLine('run.started', ' ' + targetUrl);
      openStream(currentRunId);
      refreshTabs();

      currentMaxSteps = Number(document.getElementById('maxSteps').value) || {default_steps};
      const selectedModel = document.getElementById('modelSelect').value || '';
      MODEL_NAME = selectedModel || MODEL_NAME;
      try {{
        const resp = await fetch(`/runs/${{currentRunId}}/agent/start`, {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{max_steps:currentMaxSteps, step_delay_ms:DEFAULT_DELAY, model:selectedModel}}),
        }});
        if (!resp.ok) throw new Error(`HTTP ${{resp.status}}: ${{await resp.text()}}`);
      }} catch(e) {{
        setStatus('error','failed'); setRunning(false); showError('Failed to start agent: ' + e.message);
      }}
    }}

    async function stopRun() {{
      if (!currentRunId) return;
      await fetch(`/runs/${{currentRunId}}/stop`, {{method:'POST'}});
      setStatus('stopped','');
      setRunning(false);
    }}

    async function refreshTabs() {{
      if (!currentRunId) return;
      const resp = await fetch(`/runs/${{currentRunId}}/tabs`);
      if (!resp.ok) return;
      const d = await resp.json();
      document.getElementById('tabsList').innerHTML = d.tabs.map(t =>
        `<div>${{t.is_active?'&#9654;':'&nbsp;&nbsp;'}} ${{esc(t.url.slice(0,46))}}</div>`
      ).join('') || '—';
    }}

    // ── Report UI ────────────────────────────────────────────────────
    document.getElementById('copyReportBtn').addEventListener('click', () => {{
      const text = document.getElementById('reportContent').textContent;
      navigator.clipboard.writeText(text).catch(() => {{
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      }});
      document.getElementById('copyReportBtn').textContent = '✓ Copied!';
      setTimeout(() => {{ document.getElementById('copyReportBtn').textContent = '📋 Copy Report'; }}, 2000);
    }});
    document.getElementById('printReportBtn').addEventListener('click', () => window.print());
    document.getElementById('closeReportBtn').addEventListener('click', closeReport);
    // Keyboard: R = open report, Escape = close report
    document.addEventListener('keydown', e => {{
      if (e.key === 'r' && !e.metaKey && !e.ctrlKey && allSteps.length > 0) showReport();
      if (e.key === 'Escape') closeReport();
    }});

    // ── Init ─────────────────────────────────────────────────────────
    document.getElementById('launchBtn').addEventListener('click', launch);
    document.getElementById('stopBtn').addEventListener('click', stopRun);
    document.getElementById('viewReportBtn').addEventListener('click', showReport);
    document.getElementById('pauseScrollBtn').addEventListener('click', () => {{
      scrollPaused = !scrollPaused;
      document.getElementById('pauseScrollBtn').textContent = scrollPaused ? '‖' : '↓';
    }});

    document.getElementById('targetUrl').value = DEFAULT_TARGET;
    const q = new URLSearchParams(window.location.search);
    if (q.get('target')) document.getElementById('targetUrl').value = q.get('target');
    if (q.get('autorun') === '1') setTimeout(() => {{
      if (q.get('autostart_agent') === '1') launch();
    }}, 200);

    // Load available models into dropdown
    (async function loadModels() {{
      try {{
        const resp = await fetch('/config/models');
        if (!resp.ok) return;
        const cfg = await resp.json();
        const sel = document.getElementById('modelSelect');
        sel.innerHTML = '';
        const providers = {{}};
        cfg.models.forEach(m => {{
          if (!providers[m.provider]) providers[m.provider] = [];
          providers[m.provider].push(m);
        }});
        for (const [provider, models] of Object.entries(providers)) {{
          const grp = document.createElement('optgroup');
          grp.label = provider === 'anthropic' ? '🟣 Anthropic (Claude)' : '🔵 Google (Gemini)';
          models.forEach(m => {{
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.label;
            opt.disabled = !m.available;
            if (!m.available) opt.textContent += ' (no API key)';
            if (m.id === cfg.default_model) opt.selected = true;
            grp.appendChild(opt);
          }});
          sel.appendChild(grp);
        }}
      }} catch(_) {{}}
    }})();

    setInterval(refreshTabs, 2000);
  </script>
</body>
</html>
"""
        return Response(
            content=html,
            media_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    @app.get("/engagement-dashboard", response_class=HTMLResponse)
    def engagement_dashboard() -> str:
        default_target = json.dumps(app.state.default_target_url)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Security Engagement Console</title>
  <style>
    :root {{
      --bg:#07141e; --panel:#0f2535; --card:#133147; --line:#27506b; --text:#d7e8f4;
      --muted:#93b1c6; --accent:#3eb6ff; --good:#52d273; --warn:#f6b73c; --bad:#ff6b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; color: var(--text); background: radial-gradient(circle at 20% 0%, #13354d, #07141e 45%);
      font-family: \"IBM Plex Sans\", \"Segoe UI\", sans-serif;
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 18px; }}
    .hero {{
      border: 1px solid var(--line); border-radius: 12px; padding: 18px; background: rgba(15,37,53,0.85);
      display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0.2px; }}
    p {{ margin: 0; color: var(--muted); }}
    .controls {{ display:flex; flex-wrap:wrap; gap:8px; margin-top: 14px; }}
    input, select, button {{
      border: 1px solid var(--line); background: #0c1d2b; color: var(--text); border-radius: 8px; padding: 10px 12px;
      font-size: 14px;
    }}
    button {{ cursor: pointer; background: #12496b; border-color: #1f709f; }}
    button:hover {{ background: #18608d; }}
    .grid {{ margin-top: 14px; display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }}
    .panel {{ border: 1px solid var(--line); border-radius: 12px; background: rgba(15,37,53,0.86); padding: 14px; }}
    .panel h2 {{ margin: 0 0 10px; font-size: 15px; letter-spacing: 0.4px; text-transform: uppercase; color: var(--muted); }}
    .timeline {{ max-height: 420px; overflow: auto; font-family: \"IBM Plex Mono\", monospace; font-size: 12px; }}
    .evt {{ padding: 8px 10px; border-left: 3px solid var(--accent); background: rgba(19,49,71,0.7); margin-bottom: 8px; border-radius: 0 8px 8px 0; }}
    .evt .t {{ color: var(--muted); margin-right: 8px; }}
    .metric {{ display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(39,80,107,0.45); }}
    .metric:last-child {{ border-bottom: 0; }}
    .tag {{ font-weight: 600; }}
    .good {{ color: var(--good); }} .warn {{ color: var(--warn); }} .bad {{ color: var(--bad); }}
    .finding {{ margin-bottom: 8px; padding: 10px; border-radius: 8px; background: rgba(19,49,71,0.74); border: 1px solid rgba(39,80,107,0.45); }}
    .finding h3 {{ margin: 0 0 4px; font-size: 14px; }}
    .finding p {{ margin: 0; font-size: 12px; color: var(--muted); }}
    .split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    @media (max-width: 960px) {{ .grid, .split {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>Automated Security Engagement Demo</h1>
        <p>Agentic blackbox testing with orchestration, approval gate, and executive reporting.</p>
      </div>
      <a href="/dashboard" style="color:var(--accent);text-decoration:none;font-size:13px">Open Technical Dashboard →</a>
      <a href="/ops-console" style="color:var(--good);text-decoration:none;font-size:13px;margin-left:12px">Live Ops Console →</a>
    </div>
    <div class="controls">
      <input id="target" style="min-width:340px" placeholder="https://target" />
      <select id="approvalMode">
        <option value="optional" selected>Approval: Optional</option>
        <option value="mandatory">Approval: Mandatory</option>
        <option value="none">Approval: None</option>
      </select>
      <input id="budget" type="number" min="1" step="1" value="50" title="Budget USD" />
      <button id="createBtn">Create Engagement</button>
      <button id="startBtn">Start</button>
      <button id="approveBtn">Approve & Continue</button>
      <button id="rejectBtn">Reject</button>
      <span id="engId" style="padding:10px 4px;color:var(--muted)">engagement: —</span>
    </div>
    <div class="grid">
      <div class="panel">
        <h2>Execution Timeline</h2>
        <div id="timeline" class="timeline"></div>
      </div>
      <div class="panel">
        <h2>Status</h2>
        <div id="statusMetrics"></div>
      </div>
    </div>
    <div class="split">
      <div class="panel">
        <h2>Suspected Findings</h2>
        <div id="suspected"></div>
      </div>
      <div class="panel">
        <h2>Confirmed Findings</h2>
        <div id="confirmed"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Tool Activity</h2>
      <div id="toolActivity" style="font-family:'IBM Plex Mono',monospace;font-size:12px;max-height:260px;overflow:auto;">No tool activity yet.</div>
    </div>
    <div class="panel">
      <h2>Executive Report</h2>
      <div id="report">No report generated yet.</div>
    </div>
  </div>
  <script>
    let engagementId = null;
    document.getElementById("target").value = {default_target};

    function esc(s) {{
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }}

    function findingHtml(f) {{
      return `<div class=\"finding\"><h3>${{esc(f.title || f.vuln_type)}}</h3><p>${{esc(f.endpoint || \"\")}}</p><p>severity: <span class=\"tag\">${{esc(f.severity || \"medium\")}}</span> · confidence: ${{esc(f.confidence || 0)}}</p></div>`;
    }}

    async function createEngagement() {{
      const target = (document.getElementById("target").value || "").trim();
      if (!target) return;
      const resp = await fetch("/engagements", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{
          target_url: target,
          approval_mode: document.getElementById("approvalMode").value,
          budget_usd: Number(document.getElementById("budget").value || "50")
        }})
      }});
      if (!resp.ok) return;
      const d = await resp.json();
      engagementId = d.engagement_id;
      document.getElementById("engId").textContent = `engagement: ${{engagementId}}`;
      await refreshAll();
    }}

    async function startEngagement() {{
      if (!engagementId) return;
      await fetch(`/engagements/${{engagementId}}/start`, {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{max_steps_per_agent: 12, step_delay_ms: 150}})
      }});
      await refreshAll();
    }}

    async function approveEngagement(approved) {{
      if (!engagementId) return;
      await fetch(`/engagements/${{engagementId}}/approval`, {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{approved, note: approved ? "approved from dashboard" : "rejected from dashboard"}})
      }});
      await refreshAll();
    }}

    async function refreshAll() {{
      if (!engagementId) return;
      const [engResp, evResp, repResp, tiResp] = await Promise.all([
        fetch(`/engagements/${{engagementId}}`),
        fetch(`/engagements/${{engagementId}}/events`),
        fetch(`/engagements/${{engagementId}}/report`),
        fetch(`/engagements/${{engagementId}}/tool-invocations`),
      ]);
      if (!engResp.ok) return;
      const eng = await engResp.json();
      const ev = evResp.ok ? await evResp.json() : {{events:[]}};
      const rep = repResp.ok ? await repResp.json() : {{report:null}};
      const tiData = tiResp.ok ? await tiResp.json() : {{tool_invocations:[]}};

      const s = eng.status;
      const cls = s === "completed" ? "good" : s.includes("paused") ? "warn" : s.includes("failed") ? "bad" : "";
      document.getElementById("statusMetrics").innerHTML = `
        <div class="metric"><span>status</span><span class="${{cls}}">${{esc(eng.status)}}</span></div>
        <div class="metric"><span>phase</span><span>${{esc(eng.current_phase)}}</span></div>
        <div class="metric"><span>budget used</span><span>${{Number(eng.budget?.spent_usd || 0).toFixed(3)}} / ${{Number(eng.budget?.limit_usd || 0).toFixed(2)}}</span></div>
        <div class="metric"><span>surface endpoints</span><span>${{(eng.attack_surface?.endpoints || []).length}}</span></div>
        <div class="metric"><span>suspected</span><span>${{(eng.suspected_findings || []).length}}</span></div>
        <div class="metric"><span>confirmed</span><span>${{(eng.confirmed_findings || []).length}}</span></div>
        <div class="metric"><span>tool calls</span><span>${{(tiData.tool_invocations || []).length}}</span></div>
      `;

      document.getElementById("timeline").innerHTML = (ev.events || []).slice(-120).map(x => {{
        const t = (x.ts || "").replace("T", " ").slice(0, 19);
        return `<div class="evt"><span class="t">${{esc(t)}}</span><strong>${{esc(x.type)}}</strong><div>${{esc(JSON.stringify(x.payload || {{}}))}}</div></div>`;
      }}).join("") || "<p>No events yet.</p>";

      document.getElementById("suspected").innerHTML = (eng.suspected_findings || []).map(findingHtml).join("") || "<p>No suspected findings.</p>";
      document.getElementById("confirmed").innerHTML = (eng.confirmed_findings || []).map(findingHtml).join("") || "<p>No confirmed findings.</p>";

      const invocations = tiData.tool_invocations || [];
      if (invocations.length === 0) {{
        document.getElementById("toolActivity").innerHTML = "<p style='color:var(--muted)'>No tool activity yet.</p>";
      }} else {{
        document.getElementById("toolActivity").innerHTML = invocations.slice(-50).map(ti => {{
          const status = ti.ok ? `<span class="good">ok</span>` : `<span class="bad">${{esc(ti.error || 'rejected')}}</span>`;
          const dur = ti.duration_ms != null ? `${{Number(ti.duration_ms).toFixed(0)}}ms` : "—";
          const cost = `$${{Number(ti.cost_usd || 0).toFixed(3)}}`;
          return `<div style="padding:4px 0;border-bottom:1px solid rgba(39,80,107,0.3)"><strong>${{esc(ti.tool_name)}}</strong> ${{esc(ti.target)}} → ${{status}} (${{dur}}, ${{cost}})</div>`;
        }}).join("");
      }}

      if (rep.report) {{
        const r = rep.report;
        document.getElementById("report").innerHTML = `
          <p><strong>${{esc(r.title || "Executive Report")}}</strong></p>
          <p>${{esc(r.summary || "")}}</p>
          <p><strong>Risk Overview:</strong> critical=${{r.findings_overview?.critical||0}}, high=${{r.findings_overview?.high||0}}, medium=${{r.findings_overview?.medium||0}}, low=${{r.findings_overview?.low||0}}</p>
          <p><strong>Top Risks:</strong> ${{esc((r.key_risks || []).join(" | "))}}</p>
          <p><strong>Recommendations:</strong> ${{esc((r.recommendations || []).join(" | "))}}</p>
        `;
      }}
    }}

    document.getElementById("createBtn").addEventListener("click", createEngagement);
    document.getElementById("startBtn").addEventListener("click", startEngagement);
    document.getElementById("approveBtn").addEventListener("click", () => approveEngagement(true));
    document.getElementById("rejectBtn").addEventListener("click", () => approveEngagement(false));
    setInterval(refreshAll, 1500);
  </script>
</body>
</html>"""

    return app
