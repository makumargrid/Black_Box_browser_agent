# Blackbox Agent Service

An autonomous AI-powered web application security testing platform with a governed multi-agent engagement pipeline, real offensive security tooling via HexStrike, and a cinematic live operations console.

---

## What This Is

### Phase A — Governed Multi-Agent Engagement Pipeline

Phase A is the main production system. It runs three capability agents sequentially under server-side orchestration:

```
POST /engagements  →  DiscoveryAgent  →  AccessTestAgent
    →  [HITL Approval Gate]  →  ConfirmEvidenceAgent  →  ExecutiveReport
```

- **DiscoveryAgent**: maps the attack surface (hosts, endpoints, tech stack) via HTTP probes and, when HexStrike is enabled, real tools (nmap, subfinder, katana, nuclei).
- **AccessTestAgent**: tests authentication, access control, and API exposure. With HexStrike, runs nuclei for CVE/template scanning.
- **ConfirmEvidenceAgent**: confirms suspected findings with hard evidence. Post-approval, can run sqlmap_probe via the gated ToolChannel.
- **HITL Approval Gate**: pauses before destructive confirmation steps; operator approves or rejects via API or dashboard.

All agents run server-side. The system is fully headless-capable and production-ready for Docker deployment.

### Phase B — Standalone Browser-Use Demo Agent

Phase B (`run_agent.py`, `agents/`) is a completely separate, decoupled browser-use demo. It injects an overlay sidebar into the live target browser using Playwright and provides a cinematic step-by-step view. It has no orchestrator, no budget, no audit log, and no HexStrike integration.

**Phase A and Phase B share no code and must never share imports.**

---

## HexStrike ToolChannel

The ToolChannel gives Phase-A agents access to real offensive security tools (nmap, nuclei, ffuf, gobuster, subfinder, katana, sqlmap) via HexStrike AI v6.0 — **OFF by default**, enabled with one environment variable.

Every tool call flows through the `SecurityToolGate`, which enforces:

| Guard | Rule |
|-------|------|
| Scope | Target must be within the engagement's origin |
| Approval | Gated tools (sqlmap_probe) require HITL approval |
| Budget | Spend cannot exceed `min(budget_usd, BLACKBOX_TOOL_BUDGET_HARD_CAP_USD)` |
| Audit | Every decision (pass or reject) appends an `EngagementEvent` |

The ToolChannel is **Phase-A only**. Phase B is intentionally excluded.

See [`docs/hexstrike_integration.md`](docs/hexstrike_integration.md) for the full ops guide.

---

## Phase-A UIs

Two browser interfaces serve the Phase-A engagement pipeline:

| UI | URL | When to use |
|----|-----|-------------|
| **Operations Console** (SSE live view) | `/ops-console` | Primary demo surface — cinematic, real-time, Phase-B feel |
| **Engagement Dashboard** (polling view) | `/engagement-dashboard` | Quick status check, approval controls, executive report |

The Operations Console uses [EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource) to stream `GET /engagements/{id}/stream` and displays:
- Animated glow border (blue = thinking, red = exploitation/confirm, green = success)
- Color-coded event log with PHASE / TOOL ✓ / TOOL ✗ / BUDGET / APPROVAL / CONFIRM chips
- Live Tool Activity panel showing every HexStrike invocation with timing and cost
- Findings panel, approve/reject controls, and an executive report overlay

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/your-org/blackbox-agent.git
cd blackbox-agent
uv sync
uv run playwright install chromium
```

### 2. Configure `.env`

```bash
cp .env.example .env
# Edit .env and set:
```

| Variable | Required | Notes |
|----------|----------|-------|
| `ANTHROPIC_API_KEY` | Yes | Read from `.env` file only (never terminal env) |
| `GEMINI_API_KEY` | Optional | Fallback model; read from `.env` file only |
| `BLACKBOX_HEXSTRIKE_ENABLED` | Optional | `true` to enable real tools (default: false) |
| `BLACKBOX_HEXSTRIKE_URL` | Optional | HexStrike server URL (default: `http://localhost:8888`) |
| `BLACKBOX_HEXSTRIKE_TIMEOUT_S` | Optional | Per-tool timeout seconds (default: 300) |
| `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` | Optional | Hard cap per engagement (default: 5.0) |
| `BLACKBOX_AGENT_MODEL` | Optional | Claude model slug (default: `claude-sonnet-4-6`) |
| `BLACKBOX_GEMINI_MODEL` | Optional | Gemini model slug (default: `gemini-2.5-flash`) |

Runtime settings (host, port, playwright flags) can be overridden by environment variables; API keys cannot.

---

## Run Paths

### Local service

```bash
uv run lean_agent
# Open http://localhost:8080/ops-console
```

### Demo launcher (opens Ops Console automatically)

```bash
uv run demo_blackbox --ops-console http://juice-shop:3000
# Starts the service if not already running, opens /ops-console with target pre-filled
```

### Full Docker stack (juice-shop + HexStrike + blackbox-agent)

```bash
# First: clone HexStrike into ./hexstrike/
git clone --branch v6.0 https://github.com/0x4m4/hexstrike-ai.git hexstrike

docker compose up --build
# Open http://localhost:8080/ops-console
```

The compose file enables HexStrike automatically (`BLACKBOX_HEXSTRIKE_ENABLED=true`). All API keys stay in `.env` — never in the compose file.

### Phase B standalone demo (separate, no engagement pipeline)

```bash
uv run run_agent.py  # direct browser-use agent, separate from Phase A
```

---

## API Reference

### Run/Browser API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/runs` | Create a browser run |
| `GET` | `/runs/{run_id}` | Get run state |
| `GET` | `/runs/{run_id}/tabs` | List open tabs |
| `POST` | `/runs/{run_id}/actions` | Execute a browser action |
| `GET` | `/runs/{run_id}/memory` | List stored events |
| `GET` | `/runs/{run_id}/artifacts` | List artifacts |
| `POST` | `/runs/{run_id}/agent/start` | Start the autonomous agent loop |
| `GET` | `/runs/{run_id}/agent/state` | Get agent state |
| `POST` | `/runs/{run_id}/stop` | Stop a run |
| `GET` | `/runs/{run_id}/stream` | SSE stream for run events |

### Engagement API (Phase A)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/engagements` | Create an engagement |
| `POST` | `/engagements/{id}/start` | Start the agent pipeline |
| `GET` | `/engagements/{id}` | Get engagement state |
| `GET` | `/engagements/{id}/events` | All events (polling) |
| `GET` | `/engagements/{id}/stream` | **Live SSE stream** (replays history + streams new) |
| `POST` | `/engagements/{id}/approval` | Approve or reject HITL gate |
| `GET` | `/engagements/{id}/findings` | Suspected + confirmed findings |
| `GET` | `/engagements/{id}/report` | Executive report |
| `GET` | `/engagements/{id}/tool-invocations` | ToolChannel audit log |

### UI Routes

| Path | Description |
|------|-------------|
| `/ops-console` | Operations Console (cinematic SSE live view) |
| `/engagement-dashboard` | Executive dashboard (polling) |
| `/dashboard` | Technical browser agent dashboard |

---

## Tests

```bash
uv run pytest -q
# 80 tests passing
```

---

## Architecture

See [`explanation.md`](explanation.md) for complete technical architecture, the BIE tier system, the ToolChannel guardrails, the SSE stream contract, and the Operations Console event mapping.

See [`docs/agentic_architecture_reference.md`](docs/agentic_architecture_reference.md) for component overview.
