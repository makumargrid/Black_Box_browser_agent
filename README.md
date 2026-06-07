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
- **HITL Approval Gate**: pauses before destructive confirmation steps; operator approves or rejects via API or dashboard. Both `mandatory` and `optional` modes pause correctly once and resume into `confirm_evidence` after approval (they never re-pause).

### Phase B — Standalone Browser-Use Demo Agent

Phase B (`run_agent.py`, `agents/`) is a completely separate, decoupled browser-use demo. It injects an overlay sidebar into the live target browser using Playwright. It has no orchestrator, no budget, no audit log, and no HexStrike integration.

**Phase A and Phase B share no code and must never share imports.**

---

## HexStrike ToolChannel

The ToolChannel gives Phase-A agents access to real offensive security tools (nmap, nuclei, ffuf, gobuster, subfinder, katana, sqlmap) via HexStrike AI v6.0 — **OFF by default**, enabled with one environment variable.

Every tool call flows through the `SecurityToolGate`, which enforces:

| Guard | Rule |
|-------|------|
| Scope | Target must be within the engagement's origin |
| Approval | Gated tools (sqlmap_probe) require HITL approval |
| Budget | Tool spend pool: `tool_spent_usd` must not exceed `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` |
| Cleanup | Artifact paths registered before execution; orphaned files removed on run teardown via `cleanup()` |
| Audit | Every decision (pass or reject) appends an `EngagementEvent` and publishes live to the SSE stream |

**Tool budget is separate from the engagement budget.** The engagement `budget_usd` covers LLM + browser costs. Tools have their own `tool_spent_usd` pool capped at `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` (default $5). Neither pool affects the other.

The ToolChannel is **Phase-A only**. Phase B is intentionally excluded.

See [`docs/hexstrike_integration.md`](docs/hexstrike_integration.md) for the full ops guide.

---

## Phase-A UIs

| UI | URL | When to use |
|----|-----|-------------|
| **Operations Console** (SSE live view) | `/ops-console` | Primary demo surface — cinematic, real-time, live tool activity |
| **Engagement Dashboard** (polling view) | `/engagement-dashboard` | Quick status check, approval controls, executive report |

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
# Edit .env and fill in ANTHROPIC_API_KEY (and optionally GEMINI_API_KEY)
```

See [`.env.example`](.env.example) for every available variable with descriptions.

Key variables:

| Variable | Required | Notes |
|----------|----------|-------|
| `ANTHROPIC_API_KEY` | Yes | Read from `.env` file only (never terminal env) |
| `GEMINI_API_KEY` | Optional | Fallback model; read from `.env` only |
| `BLACKBOX_HEXSTRIKE_ENABLED` | Optional | `true` to enable real tools (default: false) |
| `BLACKBOX_HEXSTRIKE_URL` | Optional | HexStrike server URL (default: `http://localhost:8888`) |
| `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` | Optional | Tool spend hard cap per engagement (default: 5.0) |

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
```

### Docker — default stack (juice-shop + blackbox-agent, no HexStrike)

Works on a clean checkout — no prerequisites beyond Docker:

```bash
docker compose up --build
# Open http://localhost:8080/ops-console
```

### Docker — full stack with HexStrike tooling

Requires cloning HexStrike first:

```bash
git clone https://github.com/0x4m4/hexstrike-ai.git hexstrike
docker compose --profile tools up --build
# Open http://localhost:8080/ops-console
```

### Phase B standalone demo (separate, no engagement pipeline)

```bash
uv run run_agent.py
```

---

## API Reference

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
| `/ops-console` | Operations Console (SSE live view) |
| `/engagement-dashboard` | Executive dashboard (polling) |
| `/dashboard` | Technical browser agent dashboard |

---

## Tests

```bash
# Install dev deps first:
pip install -e ".[dev]"
# or:
uv sync

pytest -q
# 93+ tests, zero errors
```

---

## Architecture

See [`explanation.md`](explanation.md) for complete technical architecture including the ToolChannel guardrails (C1–H4 fixes), the SSE stream contract, and the Operations Console event mapping.
