# HexStrike Integration — Ops Guide

> This document covers operational procedures for the ToolChannel / HexStrike integration in the `blackbox_service` Phase-A engagement pipeline. Phase B (`run_agent.py`, `agents/`) is intentionally excluded.

---

## Starting the Full Stack

### Prerequisites

1. Clone HexStrike AI v6.0 into a `hexstrike/` subdirectory alongside `docker-compose.yml`:

```bash
git clone --branch v6.0 https://github.com/0x4m4/hexstrike-ai.git hexstrike
```

If no `v6.0` tag exists, use the `master` branch (current stable baseline) and pin by commit SHA in your CI.

2. Ensure your `.env` file exists (the compose file volume-mounts it):

```bash
cp .env.example .env   # then fill in ANTHROPIC_API_KEY etc.
```

3. Start the full stack:

```bash
docker compose up --build
```

The compose file starts three services: `juice-shop`, `hexstrike`, and `blackbox-agent`. `blackbox-agent` waits for the `hexstrike` health check to pass before starting.

---

## Verifying HexStrike Reachability

### From the host machine

```bash
curl -s http://localhost:8888/health
# Expected: 200 OK {"status": "ok"} or similar
```

### From within the blackbox-agent container

```bash
docker exec blackbox-agent curl -s http://hexstrike:8888/health
```

### Via the blackbox-agent capability report

```bash
curl -s http://localhost:8080/health | python3 -m json.tool
# Look for: "capabilities": {"toolchannel_enabled": true}
```

If `toolchannel_enabled` is `false`, check:
- `BLACKBOX_HEXSTRIKE_ENABLED` is set to `"true"` in the compose env
- HexStrike started successfully: `docker compose logs hexstrike`

---

## What Happens on a Hung Tool

The `HexStrikeClient` uses `BLACKBOX_HEXSTRIKE_TIMEOUT_S` (default 300s / 5 min) as the HTTP request timeout per tool invocation. If a tool call hangs:

1. After the timeout, `httpx.TimeoutException` is caught internally.
2. `HexStrikeClient.invoke()` returns `{"ok": False, "error": "HexStrike invoke timed out after 300.0s: ..."}`.
3. `SecurityToolGate` receives `ok=False`, **refunds** the reserved budget, updates the `ToolInvocation` record, and appends a `tool.invoked` audit event with `ok=False`.
4. The agent receives a clean negative result and continues with its next step.
5. The engagement does NOT fail — the tool timeout is non-fatal.

To adjust the timeout:

```bash
BLACKBOX_HEXSTRIKE_TIMEOUT_S=120  # 2 minutes — faster fail for interactive runs
```

---

## Reading the Audit Log

### Via the REST API

```bash
# All engagement events (includes tool.invoked, tool.rejected)
curl -s http://localhost:8080/engagements/{ENGAGEMENT_ID}/events | python3 -m json.tool

# Structured tool invocation records
curl -s http://localhost:8080/engagements/{ENGAGEMENT_ID}/tool-invocations | python3 -m json.tool
```

### Event types

| Event type | Meaning |
|-----------|---------|
| `tool.invoked` | Tool call passed all checks and was executed (ok may be true or false) |
| `tool.rejected` | Tool call was blocked by a gate guardrail |

Each `tool.rejected` event includes a `reason` field:

| Reason | Guardrail |
|--------|-----------|
| `out_of_scope` | Target host not within engagement origin |
| `requires_hitl_approval` | Gated tool called before HITL approval |
| `budget_exhausted` | Combined tool spend would exceed cap |

### Via the Operations Console (live SSE view)

Open `http://localhost:8080/ops-console` — the "Tool Activity" panel fills in real time as the SSE stream delivers `tool.invoked` and `tool.rejected` events. This is the recommended way to monitor tool activity during a live engagement.

### Via the engagement dashboard (polling fallback)

Open `http://localhost:8080/engagement-dashboard` — the "Tool Activity" panel shows live tool invocations and polls every 1.5 seconds.

---

## Disabling HexStrike Without Rebuilding

Set the environment variable at runtime (overrides the compose default):

```bash
BLACKBOX_HEXSTRIKE_ENABLED=false docker compose up blackbox-agent
```

Or edit your `.env` file:

```
BLACKBOX_HEXSTRIKE_ENABLED=false
```

When disabled, all agents operate in BIE-only mode — behavior is identical to the pre-ToolChannel baseline.

---

## Budget and Cost Controls

| Setting | Default | Description |
|---------|---------|-------------|
| `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` | `5.0` | Per-engagement hard cap on tool spend (cannot be overridden by `budget_usd`) |
| `budget_usd` in `POST /engagements` | `50.0` | Per-engagement LLM + tool budget; effective cap = min(budget_usd, hard_cap) |

Costs are deducted atomically. A rejected call does not consume budget. A failed tool call (ok=False from HexStrike) refunds the reserved amount.

---

## Security Notes

- HexStrike requires `privileged: true` in Docker for tools like nmap that use raw sockets. Restrict this in hardened production environments.
- No secrets should appear in `docker-compose.yml`. All API keys live in `.env` (volume-mounted read-only).
- The `SecurityToolGate` is the single chokepoint between agents and HexStrike. Agents must never import `HexStrikeClient` directly.
- Gated tools (`sqlmap_probe`) require explicit HITL approval. The gate enforces this at the Python level independent of the LLM prompt.
