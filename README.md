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

> **Phase A vs Phase B — the most important distinction:**
> - `/dashboard` is **Phase B**: a standalone browser-use demo. It injects an overlay sidebar into the target browser. It has NO orchestrator, NO HexStrike tools, NO engagement pipeline. Useful for visual demos.
> - `/ops-console` and `/engagement-dashboard` are **Phase A**: the governed multi-agent engagement pipeline. This is where DiscoveryAgent/AccessTestAgent/ConfirmEvidenceAgent run and where HexStrike tools can be invoked.
>
> **Both phases require `ANTHROPIC_API_KEY` in `.env`. Without it, Phase A agents exit after 0 steps (silent success with 0 findings).**

| UI | URL | When to use |
|----|-----|-------------|
| **Operations Console** (SSE live view) | `/ops-console` | Primary demo surface — cinematic, real-time, live tool activity. Shows LLM: ON/OFF and Tools: ON/OFF badges. |
| **Engagement Dashboard** (polling view) | `/engagement-dashboard` | Quick status check, approval controls, executive report |
| **Browser Agent Dashboard** (Phase B) | `/dashboard` | Standalone browser-use demo — no tools, no pipeline |

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

### Local service with HexStrike tools (one-liner)

```bash
BLACKBOX_HEXSTRIKE_ENABLED=true BLACKBOX_HEXSTRIKE_URL=http://localhost:8888 uv run lean_agent
# Open http://localhost:8080/ops-console
# The Tools badge in the header shows ON/OFF based on reachability
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

All three services (`juice-shop`, `hexstrike`, `blackbox-agent`) share the `bbnet` bridge network so HexStrike can resolve `juice-shop` by name.

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
# 116+ tests, zero errors
```

---

## Troubleshooting: No Tool Calls?

> **Quick check: Which URL are you using?**
> - `/dashboard` = **Phase B** (standalone browser-use demo). Phase B has **NO HexStrike tools** by design, ever. This is the "cinematic hacking overlay" demo.
> - `/ops-console` or `/engagement-dashboard` = **Phase A** (governed engagement pipeline). This is where tools run.
>
> If you ran against an external site like `blogger.com` via `/dashboard`, that is Phase B — it will never call nmap/nuclei/sqlmap regardless of any configuration.

If you are on Phase A and still see no tool calls:

0. **Check the LLM badge** (`LLM: OFF` = `ANTHROPIC_API_KEY` missing).
   - Without the key, agents exit after 0 steps and the engagement reaches "completed" with 0 findings and no explanation.
   - Check `GET /health` → `capabilities.llm_key_configured`.
   - Check `GET /engagements/{id}` → `last_error` field for the clear message.
   - Check `GET /engagements/{id}/events` for `phase.warning` events with `reason=no_llm_key`.
   - Fix: add `ANTHROPIC_API_KEY=<your-key>` to `.env` and restart.

1. **Check the Tools badge** in the Ops Console header (`/ops-console`).
   - `Tools: ON` (green) = HexStrike is configured AND reachable.
   - `Tools: OFF` = HexStrike is disabled or unreachable.

2. **Check `hexstrike_reachable`** in `GET /health` → `capabilities`:
   ```bash
   curl http://localhost:8080/health | python3 -m json.tool | grep -E "tool_channel|hexstrike"
   ```

3. **Check logs** for the startup line:
   ```
   ToolChannel: ENABLED (HexStrike http://..., reachable=True)
   ```
   If it says `reachable=False`, HexStrike started but isn't listening yet.

4. **Target host format** — when tools are enabled but rejected:
   - Check for `tool.rejected` events in `GET /engagements/{id}/events`.
   - `out_of_scope` → reissue the tool with the correct host format:
     - nmap/subfinder: use the bare hostname (`juice-shop`, not `http://...`)
     - nuclei/katana/sqlmap: use the full URL with port (`http://juice-shop:3000`)
   - `requires_hitl_approval` → approve the engagement first.

5. **Docker only**: Ensure all services are on the same network (`bbnet`).
   ```bash
   docker compose --profile tools config | grep bbnet
   ```

---

## Architecture

See [`explanation.md`](explanation.md) for complete technical architecture including the ToolChannel guardrails (C1–H4 fixes), the SSE stream contract, and the Operations Console event mapping.
