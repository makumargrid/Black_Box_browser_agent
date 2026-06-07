# Agentic Security Demo Reference

## Goal
Deliver a demo-safe, agentic blackbox testing system with:
- Orchestrator-managed engagement lifecycle
- Three capability agents (Discovery, Access/Test, Confirm/Evidence)
- Unified Browser Interaction Engine (BIE)
- HexStrike ToolChannel for real offensive security tooling
- Approval gate and executive reporting UI
- Cinematic SSE Operations Console for live observation

## Runtime Shape
- Existing run/action engine remains active: `BlackboxService` + Playwright/InMemory runtime.
- New engagement layer wraps this engine and coordinates multi-agent execution.
- HexStrike ToolChannel is an optional parallel channel (OFF by default).

## Core Components

| Component | File | Purpose |
|-----------|------|---------|
| Orchestrator | `blackbox_service/orchestrator.py` | Engagement state machine, agent dispatch, SecurityToolGate wiring |
| BIE | `blackbox_service/bie/engine.py` | Tiered browser/HTTP abstraction (Tier 1 httpx, Tier 2 Playwright, Tier 4 browser-use) |
| Agents | `blackbox_service/agents_v2/` | DiscoveryAgent, AccessTestAgent, ConfirmEvidenceAgent |
| Models | `blackbox_service/engagement_models.py` | EngagementRecord, findings, report, ToolInvocation |
| ToolChannel | `blackbox_service/toolchannel/` | HexStrikeClient (transport) + SecurityToolGate (policy) |
| Event Bus | `blackbox_service/engagement_bus.py` | Thread-safe SSE fan-out for per-engagement consumers |
| API | `blackbox_service/api.py` | `/engagements/*`, `/stream`, `/ops-console`, `/static/*` |

## Engagement Flow
1. `POST /engagements` to create an engagement.
2. `POST /engagements/{id}/start` to run Discovery → Access/Test.
3. If tools enabled, DiscoveryAgent runs nmap/subfinder/katana/nuclei; AccessTestAgent runs nuclei_scan.
4. Optional approval pause if `approval_mode=mandatory` OR `approval_mode=optional` with findings AND `approval_granted=False`. Pauses exactly once — the second pass (after approval) has `approval_granted=True` and skips the pause.
5. `POST /engagements/{id}/approval` to continue (or reject).
6. ConfirmEvidenceAgent runs; if approved and tools enabled, can run sqlmap_probe (gated).
7. Executive report generated; engagement reaches `completed`.

## SSE Stream
- `GET /engagements/{id}/stream` — replays history then streams live events enriched with phase/status/budget snapshot (includes `tool_spent`).
- Powered by `EngagementEventBus` (one `threading.Queue` per consumer, drained via `asyncio.sleep` polling).
- Tool events (`tool.invoked`, `tool.rejected`) now flow through `SecurityToolGate._emit()` → `event_sink` → `orchestrator._event()` → bus, so they appear live in the Ops Console.
- Used by the Operations Console; additive — existing `/events` polling endpoint unchanged.

## UI Routes

| Route | Type | Description |
|-------|------|-------------|
| `/ops-console` | SSE live view | Primary demo surface — cinematic, Phase-B aesthetic, glow border |
| `/engagement-dashboard` | Polling view | Executive dashboard, approval controls, findings summary |
| `/dashboard` | Technical view | Browser agent dashboard, SSE run stream |
| `/static/*` | Static files | ops_console.css / ops_console.js |

## Demo Run Path
```bash
uv run demo_blackbox --ops-console [target_url]
# or:
docker compose up --build
# then open http://localhost:8080/ops-console
```

## Notes
- Anthropic mode remains fail-fast by default for Tier-4 AI navigation.
- Tier 3/5 BIE adapters are intentionally marked unavailable in MVP runtime.
- HexStrike and SecurityToolGate are Phase-A only; Phase B is excluded by design.
- All tool calls flow agent → SecurityToolGate → HexStrikeClient → HexStrike:8888. No agent calls HexStrikeClient directly.
