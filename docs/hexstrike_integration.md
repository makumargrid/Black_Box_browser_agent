# HexStrike Integration — Ops Guide

> This document covers operational procedures for the ToolChannel / HexStrike integration in the `blackbox_service` Phase-A engagement pipeline. Phase B (`run_agent.py`, `agents/`) is intentionally excluded.

---

## Starting the Full Stack

### Default stack (no HexStrike required)

A clean checkout works immediately — no prerequisites beyond Docker:

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
docker compose up --build
# Open http://localhost:8080/ops-console
```

This starts `juice-shop` and `blackbox-agent`. HexStrike is NOT started. The system runs in BIE-only mode (no real security tools, graceful degradation).

### Full stack with HexStrike tooling

Requires cloning HexStrike first:

```bash
git clone --branch v6.0 https://github.com/0x4m4/hexstrike-ai.git hexstrike
# If no v6.0 tag: git clone https://github.com/0x4m4/hexstrike-ai.git hexstrike
cp .env.example .env   # fill in ANTHROPIC_API_KEY
docker compose --profile tools up --build
# Open http://localhost:8080/ops-console
```

This starts three services: `juice-shop`, `hexstrike`, and `blackbox-agent`.

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

Tool spend uses a **separate pool** from the engagement LLM/browser budget:

| Setting | Default | Description |
|---------|---------|-------------|
| `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` | `5.0` | Hard cap for `tool_spent_usd` pool (tool-only, not shared with LLM costs) |
| `budget_usd` in `POST /engagements` | `50.0` | Engagement-wide LLM + browser budget; **not** consulted by SecurityToolGate |

- `EngagementRecord.tool_spent_usd` tracks cumulative tool spend for the engagement.
- Deductions are atomic (under `threading.Lock`). Concurrent calls cannot both pass the same budget check.
- A rejected call does not consume budget. A failed tool call (ok=False from HexStrike) refunds the reserved amount from `tool_spent_usd`.
- Both pools are visible in the Ops Console STATUS panel and in the SSE stream's `budget` snapshot.

---

## What cleanup() Does on Crash/Kill

Before each tool execution, `SecurityToolGate` registers the expected output artifact path in an internal `_pending` dict. After execution:
- On success: key is removed from `_pending` (artifact is real/tracked).
- On failure: the expected file (if created) is removed from disk; key removed from `_pending`.

`cleanup()` is called in the orchestrator's `finally` block on every run exit (completion, failure, OR approval-pause). It iterates any remaining `_pending` entries, best-effort removes their files (logging what it removes), and clears `_pending`. This guarantees no orphaned artifact files survive a killed or crashed run.

---

## Security Notes

- HexStrike requires `privileged: true` in Docker for tools like nmap that use raw sockets. Restrict this in hardened production environments.
- No secrets should appear in `docker-compose.yml`. All API keys live in `.env` (volume-mounted read-only).
- The `SecurityToolGate` is the single chokepoint between agents and HexStrike. Agents must never import `HexStrikeClient` directly.
- Gated tools (`sqlmap_probe`) require explicit HITL approval. The gate enforces this at the Python level independent of the LLM prompt.

---

## Troubleshooting: No Tool Calls in Engagements?

If engagements run without any tool activity, check in order:

1. **Tools badge in Ops Console** — `/ops-console` header shows `Tools: ON` (green) if `tool_channel_enabled=true` AND `hexstrike_reachable=true`. If `Tools: OFF`, tools won't run.

2. **Check `/health`** for reachability:
   ```bash
   curl http://localhost:8080/health | python3 -m json.tool | grep -E "tool_channel|hexstrike"
   ```
   Expected: `"tool_channel_enabled": true, "hexstrike_reachable": true`

3. **Check startup logs** for:
   ```
   ToolChannel: ENABLED (HexStrike http://..., reachable=True)
   ```

4. **Check for `tool.rejected` events** in `GET /engagements/{id}/events`:
   - `out_of_scope` → the agent used the wrong target format:
     - `nmap_scan` / `subfinder_enum`: use bare hostname (`juice-shop`)
     - `nuclei_scan` / `katana_crawl` / `sqlmap_probe`: use full URL with port (`http://juice-shop:3000`)
   - `budget_exhausted` → `tool_spent_usd` hit `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD`
   - `requires_hitl_approval` → approve the engagement before sqlmap runs

5. **Docker network** — all three services must share the `bbnet` network:
   ```bash
   docker compose --profile tools config | grep bbnet
   # Should show bbnet under each service's networks
   ```
   If HexStrike can't resolve `juice-shop`, it's likely a missing shared network.
