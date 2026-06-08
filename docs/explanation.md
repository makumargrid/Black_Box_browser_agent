# Blackbox Agent — Complete Technical Reference (Current State)

> Accurate to the codebase as of the latest commit on phase-a.
> Includes honest bottleneck analysis, token limits, and hardcoded-vs-intelligent breakdown.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Map](#2-architecture-map)
3. [Token and Resource Limits — Every Number](#3-token-and-resource-limits--every-number)
4. [Hardcoded vs. Intelligence — Honest Assessment](#4-hardcoded-vs-intelligence--honest-assessment)
5. [Tool-by-Tool Step Map](#5-tool-by-tool-step-map)
6. [The Agent Loop — AgentBase](#6-the-agent-loop--agentbase)
7. [DiscoveryAgent](#7-discoveryagent)
8. [AccessTestAgent](#8-accesstestagent)
9. [ConfirmEvidenceAgent](#9-confirmevidenceagent)
10. [Browser Interaction Engine (BIE)](#10-browser-interaction-engine-bie)
11. [SecurityToolGate](#11-securitytoolgate)
12. [HexStrikeClient and the MCP Protocol](#12-hexstrikeclient-and-the-mcp-protocol)
13. [HexStrike Architecture](#13-hexstrike-architecture)
14. [EngagementOrchestrator](#14-engagementorchestrator)
15. [BlackboxService](#15-blackboxservice)
16. [Runtime Layer](#16-runtime-layer)
17. [SQLite Event Store](#17-sqlite-event-store)
18. [Event Bus and SSE Streaming](#18-event-bus-and-sse-streaming)
19. [API Layer](#19-api-layer)
20. [Ops Console Frontend](#20-ops-console-frontend)
21. [Settings and Configuration](#21-settings-and-configuration)
22. [Budget System](#22-budget-system)
23. [HITL Approval Gate](#23-hitl-approval-gate)
24. [Dynamic Tool Discovery](#24-dynamic-tool-discovery)
25. [Remaining Bottlenecks](#25-remaining-bottlenecks)
26. [Phase A vs Phase B](#26-phase-a-vs-phase-b)
27. [Testing Architecture](#27-testing-architecture)
28. [End-to-End Flow — Complete Example](#28-end-to-end-flow--complete-example)

---

## 1. System Overview

The Blackbox Agent is an autonomous security engagement platform. Given a target URL,
it uses AI-driven agents to perform blackbox penetration testing — mapping the attack
surface, testing for all classes of vulnerabilities, and producing a structured report —
without any prior knowledge of the target's internals.

**Phase A** (`blackbox_service/`): Full enterprise service. Three specialized AI agents in
sequence, 150+ real security tools via HexStrike, HITL approval gate, SQLite persistence,
SSE real-time streaming. Run with `uv run blackbox-agent` + Docker.

**Phase B** (`agents/`, `run_agent.py`): Standalone browser-use script. Single agent,
no tools, no database, no pipeline. Run with `python run_agent.py <url>`.

They share zero code and zero imports.

---

## 2. Architecture Map

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Phase A Service                              │
│                                                                      │
│  Browser/CLI ─── FastAPI (api.py) ─── EngagementOrchestrator        │
│       │                │                      │                      │
│       │          BlackboxService          [Agent Pipeline]           │
│       │                │              Discovery→AccessTest→Confirm   │
│    SSE/HTTP   ┌────────┴────┐                 │                      │
│       │  SQLiteStore    Runtime           AgentBase.run()            │
│  Ops Console      │   (Playwright/            │                      │
│  (browser)    EventBus  InMemory)     BIE ──── SecurityGate          │
│                   │                     │          │                 │
│                   └─── SSE ─────────────┘   HexStrikeClient          │
│                                                    │                 │
│              HexStrike Docker                MCP JSON-RPC            │
│         ┌──────────────────┐           POST :8001/mcp               │
│         │  Flask :8888     │ ──────────────────────┘                 │
│         │  FastMCP :8001   │                                         │
│         │  151 tools       │                                         │
│         └──────────────────┘                                         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Token and Resource Limits — Every Number

This section lists **every hardcoded numerical limit** in the system so you know exactly
where the ceilings are.

### LLM / API Limits

| Limit | Value | File / Line | Effect if hit |
|-------|-------|-------------|--------------|
| LLM max_tokens per call | **2048** | `agents_v2/base.py:176` | Response truncated — agent returns `done=True` gracefully |
| LLM timeout | **45 seconds** | `agents_v2/base.py:190` | Returns `{done:True, thought:"LLM call failed"}` |
| Tier 4 (browser-use) LLM max_tokens | **4096** | `bie/engine.py:244` | Tier 4 response truncated |
| Tier 4 timeout (browser-use) | **300 seconds** | `bie/engine.py:60` | `fail_fast=True` → returns negative result |

### Agent Step Limits

| Limit | Value | File | Notes |
|-------|-------|------|-------|
| Max steps per agent (default) | **20** | `engagement_models.py:StartEngagementRequest` | Configurable via UI / API |
| Max steps per agent (confirm phase after approval) | **15** | `orchestrator.py:213` | Hardcoded in `approve()` — enough for 7 findings × 2 steps |
| Step delay (default) | **200 ms** | `engagement_models.py` | Configurable |
| Step delay (confirm after approval) | **200 ms** | `orchestrator.py:213` | Same as engagement default |

### Budget Limits

| Limit | Value | File | Notes |
|-------|-------|------|-------|
| Default engagement LLM budget | **$50.00** | `engagement_models.py:24` | Set at creation; overridable |
| Default tool-only hard cap | **$5.00** | `api.py:49`, `.env` var | Separate from LLM budget |
| Budget warn threshold | **80%** | `engagement_models.py:26` | Emits budget.warning event |
| Budget pause threshold | **95%** | `engagement_models.py:27` | Pauses engagement |

### Tool Costs (estimates, not real money)

| Tool | Cost per call | Notes |
|------|-------------|-------|
| nmap_scan / nmap | $0.020 | Port scan |
| nuclei_scan / nuclei | $0.050 | Template scan |
| katana_crawl / katana | $0.030 | Web crawl |
| subfinder_enum / subfinder | $0.010 | DNS-only |
| sqlmap_probe / sqlmap | $0.100 | Most expensive — database exploitation |
| ffuf, gobuster | $0.020 | Directory brute force |
| Any unknown tool | $0.050 | Default fallback |

### Caching and Polling

| Limit | Value | File | Notes |
|-------|-------|------|-------|
| HexStrike reachability cache TTL | **30 seconds** | `orchestrator.py:84` | Tool list also refreshed here |
| Ops Console badge refresh | **30 seconds** | `ops_console.js` | JS `setInterval` |
| SSE poll interval | **50 ms** | `stream.py:37`, `api.py:362` | Both event buses |

### Report Limits

| Limit | Value | File | Notes |
|-------|-------|------|-------|
| Key risks in executive report | **20** | `orchestrator.py:459` | Increased from 8 |
| Dynamic tool cap in system prompt | **40 tools** | `discovery.py:_build_dynamic_tools_section` | Caps prompt length; all 151 still in allowed_actions |
| Recent observations shown to LLM | **last 6** | All agents' `plan_next()` | Older observations not visible to LLM |
| BIE page content max chars | **4000** | `runtime.py:128`, `runtime.py:338` | Truncates page body |

### MCP / HexStrike

| Limit | Value | File | Notes |
|-------|-------|------|-------|
| HexStrike health check timeout | **5 seconds** | `hexstrike_client.py:47` | Fast fail |
| MCP list_tools timeout | **10 seconds** | `hexstrike_client.py:65` | Falls back to /health |
| MCP tool invocation timeout | **300 seconds** | `hexstrike_client.py` | Per `BLACKBOX_HEXSTRIKE_TIMEOUT_S` |
| MCP consumer queue max size | **512 events** | `engagement_bus.py:49` | Drops on overflow (slow consumer) |

---

## 4. Hardcoded vs. Intelligence — Honest Assessment

This is a complete, honest map of what the LLM decides versus what the code forces.

### ✅ Fully LLM-Driven (no hardcoded constraints on output)

| Decision | Agent | Notes |
|----------|-------|-------|
| Which tool to call next | All agents | LLM chooses from `allowed_actions` list |
| What parameters to pass to tools | All agents | LLM provides all params including target, depth, severity |
| Which URL paths to probe | Discovery | LLM decides what to probe and in what order |
| Which vulnerability class to test | AccessTest | Open-ended — LLM applies full security knowledge |
| When to mark done=true | All agents | LLM decides when coverage is sufficient |
| What "thought" / hypothesis to write | All agents | LLM generates free text |
| Which finding to confirm first | ConfirmEvidence | LLM prioritizes based on suspected findings list |

### ⚠️ LLM-Suggested but Code-Guided (prompt tells LLM what to prefer)

| Decision | Where guidance comes from | What LLM can still override |
|----------|--------------------------|----------------------------|
| Recon tool order (ports → subdomains → crawl → vuln scan) | Discovery system prompt | LLM CAN skip steps if it sees no value |
| Context-aware prioritization (login found → test auth) | AccessTest system prompt | LLM CAN skip if context suggests irrelevant |
| Retry format for out_of_scope errors | Both agents' prompts | LLM CAN choose not to retry |
| Use sqlmap for SQLi suspects | Confirm prompt | LLM CAN use other tools |

### 🔴 Fully Hardcoded (LLM has NO control over these)

| Item | Value | File | Reason it's hardcoded |
|------|-------|------|----------------------|
| Phase order | Discovery → AccessTest → Confirm | `orchestrator.py` | Logical dependency (must discover before attacking) |
| HITL approval gate | Before ConfirmEvidence only | `orchestrator.py` | Policy decision, not intelligence |
| Gated tools | sqlmap, metasploit, exploit | `security_gate.py` | Safety — destructive tools require human sign-off |
| Severity cap pre-approval | Max "medium" | `access_test.py:18-22` | Policy — no high/critical without human review |
| Scope enforcement | Target must match engagement host | `security_gate.py` | Safety — cannot scan unrelated systems |
| Budget hard cap | $5 tool budget | SecurityToolGate | Cost control |
| Finding deduplication | SHA-1 of vuln_type+endpoint | `access_test.py:_add_suspected` | Prevents duplicate reports |
| Admin/API/IDOR auto-detection | Keyword matching in http_get response | `access_test.py:_after_observation` | Supplementary to LLM — catches what LLM might miss |
| Finding severity values | high/medium/high for the 3 auto-detected types | `access_test.py:238-280` | Fixed calibration |
| Max steps in confirm-after-approval | 15 steps | `orchestrator.py:213` | Hard minimum; not user-configurable at this point |
| Report key risks cap | 20 | `orchestrator.py:459` | Prevents infinite-length reports |
| LLM `max_tokens` | 2048 | `base.py:176` | API cost control + response size |
| MCP settings (stateless+json) | True | `hexstrike_mcp.py` | Required for Docker HTTP client compatibility |

### The "last 6 observations" problem

Every call to `plan_next()` shows the LLM only the last 6 observations:
```python
"recent_observations": [... for o in observations[-6:]]
```

This means on step 15, the LLM has forgotten what happened on steps 1-9.
`tools_already_called` dict compensates for this (shows ALL tools used, not just recent 6),
but the LLM cannot "remember" a specific response from step 2.

**Impact:** The LLM might re-probe the same endpoint via different means, or miss context
from early discovery. This is the biggest intelligence gap in the current design.

---

## 5. Tool-by-Tool Step Map

### Discovery Phase — what runs at each step

| Step type | Tool/Action | Who triggers it | Hardcoded or LLM? |
|-----------|-------------|-----------------|------------------|
| Port scan | `nmap_scan` | LLM | LLM choice — will usually start here for external targets |
| Subdomain enum | `subfinder_enum`, `amass`, `dnsx` | LLM | LLM choice — if target is a domain name |
| Deep crawl | `katana_crawl` | LLM | LLM choice — replaces multiple http_get calls |
| CVE scan | `nuclei_scan` | LLM | LLM choice — checks for known vulnerabilities |
| Path probing | `http_get` | LLM | LLM choice — /robots.txt, /.env, /api, /admin, etc. |
| Page read | `get_page_content` | LLM | LLM choice — understand SPA vs server-rendered |
| Navigate | `navigate` | LLM | LLM choice — follow redirects, check auth pages |
| Any HexStrike tool | Any of 151 tools | LLM | LLM choice — full catalog available |
| Link extraction | Code (regex) | Automatic | Hardcoded — `_LINK_RE` runs on every http_get result |
| JS API path extraction | Code (regex) | Automatic | Hardcoded — `_PATH_RE` finds `/api/...` patterns in responses |
| Tech stack detection | Code | Automatic | Hardcoded — reads `Server:` header from every http_get |

**Fallback (HexStrike offline):** Only `http_get`, `get_page_content`, `navigate` available.
LLM still decides which paths to probe, but has no scanning tools.

### Access Test Phase — what runs at each step

| Step type | Tool/Action | Who triggers it | Hardcoded or LLM? |
|-----------|-------------|-----------------|------------------|
| Auth testing | `navigate` + `get_page_content` + `ai_navigate` | LLM | LLM chooses approach based on login form complexity |
| Injection testing | `http_get` with crafted payloads | LLM | LLM constructs all payloads (SQL, XSS, SSTI, etc.) |
| API probing | `http_get` | LLM | LLM probes endpoints without auth |
| Tool scans | Any of 151 HexStrike tools | LLM | LLM decides which scan tools to run |
| Screenshot evidence | `snapshot` | LLM | LLM takes snapshot when it has evidence |
| Admin content auto-detection | Code keyword match | Automatic | Hardcoded — runs after every `/admin` http_get |
| API sensitive data auto-detection | Code keyword match | Automatic | Hardcoded — runs after every `/api` http_get |
| IDOR auto-detection | Code ID increment | Automatic | Hardcoded — probes +1 adjacent ID on numeric URLs |
| nuclei severity cap | Code | Automatic | Hardcoded — caps severity at "medium" pre-approval |

**Keywords used in auto-detection:**

Admin content: `dashboard, users, settings, configuration, manage, panel, admin panel, system, analytics, user management, role, permission, privilege, audit log, system log, backup, database, cache, queue, scheduler, cronjob, worker, dequeue`

Sensitive API data: `password, secret, token, email, ssn, credit_card, private, internal, user_id, session, api_key, apikey, bearer, jwt, auth_token, access_token, refresh_token, private_key, hash, md5, sha256, sha1`

IDOR indicators: `username, email, name, address, phone, account, profile, order, balance, user, record, data, result, invoice, payment, transaction, subscription, message, notification, document, attachment`

### Confirm Evidence Phase — what runs at each step

| Step type | Tool/Action | Who triggers it | Hardcoded or LLM? |
|-----------|-------------|-----------------|------------------|
| Finding re-test | `http_get` | LLM | LLM retests each suspected endpoint |
| Navigate to endpoint | `navigate` | LLM | LLM navigates to confirm in browser |
| Screenshot evidence | `snapshot` | LLM | LLM takes screenshot; auto-tagged if untagged |
| SQLi confirmation | `sqlmap_probe` | LLM (post-approval) | LLM decides to run sqlmap; gate enforces approval |
| Any post-approval tool | From 151 HexStrike tools | LLM | LLM picks best tool for finding type |
| Finding linkage | Code (hypothesis tags) | Automatic | Hardcoded — `confirm:`, `evidence:`, `sqlmap_confirm:` tags |
| False positive classification | Code | Automatic | Hardcoded — `confirm_ok[fid]=False` → false_positive |
| Snapshot auto-tagging | Code | Automatic | Hardcoded — if LLM omits tag, code assigns to current finding |

---

## 6. The Agent Loop — AgentBase

**File:** `blackbox_service/agents_v2/base.py`

Every agent (Discovery, AccessTest, ConfirmEvidence) inherits from `AgentBase`. The loop
is identical for all three — only `plan_next()` and `_after_observation()` differ per agent.

```python
def run(ctx: AgentContext) -> dict:
    local_state = self.initialize_state(ctx)
    observations = []
    effective_tool_names = self._effective_tool_names(ctx)  # hardcoded + HexStrike tools

    for _ in range(ctx.max_steps):              # hard cap, default 20
        step = self.plan_next(ctx, local_state, observations)  # LLM CALL HERE
        if step.done:
            break

        if step.action_type in effective_tool_names:
            result = self._invoke_tool(step.action_type, step.params)  # → Gate → HexStrike
        else:
            result = self._bie.request(BIERequest(...))                # → HTTP or Playwright

        observations.append({...})
        self._after_observation(local_state, observations[-1])  # update state
        time.sleep(ctx.step_delay_ms / 1000)                    # throttle
        self._step_sink("agent.step", {...})                     # → SSE → Ops Console

    return self.summarize(ctx, local_state, observations)
```

### How the LLM call works — `_call_llm()`

1. Checks for API key — if missing, returns `{done:True}` immediately (graceful fail)
2. Builds payload:
   ```python
   {"model": ctx.anthropic_model, "max_tokens": 2048, "system": prompt, "messages": [...]}
   ```
3. Calls `POST https://api.anthropic.com/v1/messages` via raw httpx (no SDK)
4. Extracts first JSON object from response text using `re.search(r"\{.*\}", text, re.DOTALL)`
5. Falls back to `{done:True}` if JSON parsing fails

### AgentContext — what every agent knows

```python
@dataclass
class AgentContext:
    engagement_id: str
    run_id:        str        # browser session ID
    target_url:    str        # normalized (always has scheme)
    max_steps:     int = 20
    step_delay_ms: int = 200
    state:         dict       # discovery_endpoints, available_tools, etc.
    anthropic_api_key: str
    anthropic_model:   str    # e.g. "claude-sonnet-4-6"
```

### AgentStep — the LLM's decision

```python
@dataclass
class AgentStep:
    done:        bool      # terminate loop?
    goal:        str       # LLM's "thought" (shown in Ops Console)
    action_type: str       # what to execute: "nmap_scan", "http_get", "navigate", etc.
    params:      dict      # parameters for the action
    note:        str       # LLM's "hypothesis" (used for finding linkage in ConfirmEvidence)
```

### `_effective_tool_names()` — dynamic expansion

```python
def _effective_tool_names(self, ctx: AgentContext) -> frozenset[str]:
    dynamic = frozenset(
        t["name"] for t in ctx.state.get("available_tools", [])
        if isinstance(t, dict) and t.get("name")
    )
    return self._TOOL_ACTION_NAMES | dynamic
```

**Hardcoded fallbacks per agent:**
- DiscoveryAgent: `{"nmap_scan", "subfinder_enum", "katana_crawl", "nuclei_scan"}`
- AccessTestAgent: `{"nuclei_scan"}`
- ConfirmEvidenceAgent: `{"sqlmap_probe"}`

When HexStrike is online, `dynamic` expands to all 151 tools — the LLM sees and can use all of them.

---

## 7. DiscoveryAgent

**File:** `blackbox_service/agents_v2/discovery.py`  
**Purpose:** Map the attack surface — endpoints, hosts, ports, tech stack.  
**Output:** `endpoints[]`, `hosts[]`, `tech_stack[]`, `nuclei_findings[]`

### System prompt philosophy (current — open-ended)

The prompt describes tool **categories** (port scanners, subdomain finders, crawlers, vuln scanners)
and says "choose tools appropriate to the target type." It does NOT say "use nmap first."
The LLM decides the order based on the target.

For a web app with a domain name, LLM typically does:
1. nmap_scan or autorecon (ports)
2. subfinder_enum (subdomains)
3. katana_crawl (endpoints)
4. nuclei_scan (CVEs)
5. http_get on interesting paths

For an IP with no domain:
1. nmap_scan (ports)
2. masscan (fast all-ports)
3. http_get on discovered services

### `_after_observation()` — automatic parsing

Code runs AFTER every observation regardless of LLM decisions:

```
nmap output     → extracts hosts (IPs) + service banners → tech_stack
subfinder output → extracts subdomains → hosts
katana output   → extracts URLs → endpoints
nuclei output   → extracts findings → nuclei_findings (NOT endpoints)
http_get        → extracts href= links via regex → seen_urls
                → extracts /api/... patterns via regex → endpoints
                → reads Server: header → tech_stack
```

---

## 8. AccessTestAgent

**File:** `blackbox_service/agents_v2/access_test.py`  
**Purpose:** Test for ALL vulnerability classes. Produces `SuspectedFinding[]`.

### System prompt philosophy (current — open-ended)

The prompt lists vulnerability CATEGORIES, not test cases. It tells the LLM:
- "Test ALL relevant vulnerability classes based on what Discovery found"
- "Injection: SQL, NoSQL, LDAP, XPath, command, template injection"
- "XSS: reflected, stored, DOM-based"
- "Authentication flaws, session management, access control, SSRF, XXE, CORS..."
- "Prioritize based on context: login forms → test auth, numeric IDs → test IDOR..."

The LLM is NOT told "do these 5 things in this order." It applies its full knowledge.

### Auto-detection in `_after_observation()`

This runs AFTER every `http_get` observation (hardcoded in Python, not LLM-driven):

**Admin route pattern:**
```
if "/admin" in url AND status_code==200
  AND body contains admin keywords
  AND NOT login page
  AND NOT redirect page
  → SuspectedFinding(vuln_type="broken_access_control", severity="high", confidence=7)
```

**API data exposure pattern:**
```
if "/api" in url AND status_code==200
  AND body contains sensitive data keywords
  AND NOT login page
  → SuspectedFinding(vuln_type="missing_auth_api", severity="medium", confidence=5)
```

**IDOR pattern:**
```
if URL contains a numeric ID AND status_code==200
  AND body contains record data keywords
  AND NOT login page
  → SuspectedFinding(vuln_type="idor", severity="high", confidence=6)
  (endpoint set to URL with ID+1, suggesting the test to run)
```

**These patterns are supplementary.** The LLM may also flag these independently.
Deduplication via SHA-1 prevents double-reporting.

### Severity cap (hardcoded pre-approval policy)

```python
def _cap_severity_pre_approval(severity: str) -> str:
    # critical (4) and high (3) are above medium (2)
    if _SEVERITY_ORDER.get(severity, 0) > _SEVERITY_ORDER["medium"]:
        return "medium"
    return severity
```

All nuclei findings are capped at "medium" before human approval. After approval,
ConfirmEvidenceAgent can escalate to high/critical during confirmation.

---

## 9. ConfirmEvidenceAgent

**File:** `blackbox_service/agents_v2/confirm_evidence.py`  
**Purpose:** Re-test each suspected finding. Produces `ConfirmedFinding[]`.

### Hypothesis tagging protocol

The agent uses the `hypothesis` JSON field as a structured tag:

| Tag format | Meaning | Code effect |
|-----------|---------|------------|
| `confirm:<fid>` | This http_get is a re-test of finding fid | status==200 → `confirm_ok[fid]=True` |
| `evidence:<fid>` | This snapshot is evidence for finding fid | If confirm_ok → ConfirmedFinding; else FalsePositive |
| `sqlmap_confirm:<fid>` | sqlmap output links to this finding | Processes sqlmap result as ConfirmedFinding |

**Why this matters:** Without these tags, there is no way to know which action confirmed which finding. The LLM must use them correctly. If it forgets, the code auto-tags snapshots to the current finding index.

### Post-approval tool selection

After HITL approval, all 151 HexStrike tools are available. The system prompt now says:
- "SQL injection suspects → sqlmap_probe"
- "XSS suspects → dalfox or xsser if available"
- "Other suspects → best tool from allowed_actions"

---

## 10. Browser Interaction Engine (BIE)

**File:** `blackbox_service/bie/engine.py`

### Tier routing

| Tier | Actions | Backend | Cost | Use case |
|------|---------|---------|------|---------|
| 1 | `http_get`, `http_post`, `http_probe` | httpx (no browser) | $0.0001 | Fast HTTP probing |
| 2 | `navigate`, `click`, `fill`, `snapshot`, `get_page_content`, etc. | Playwright Chromium | $0.001 | Real browser interaction |
| 4 | `ai_navigate` | browser-use + Claude | $0.02 | Complex flows (OAuth, CAPTCHA) |

Tier 3 and 5 are defined in the codebase but not implemented (return error).

### Middleware delay (anti-detection)

Every BIE request adds randomized delay: `max(0.05, min(gauss(0.20, 0.12), 0.8))`
Mean 200ms, clamped 50ms–800ms. Mimics human browsing speed.

---

## 11. SecurityToolGate

**File:** `blackbox_service/toolchannel/security_gate.py`

### Five mandatory checks in order

Every tool call goes through all five. Any failure rejects the call and records audit event.

**1. Scope** — target host must match engagement origin:
```python
target = _extract_scope_target(params)
# checks: target → url → domain → host → site keys
# handles www normalization: www.example.com ≡ example.com
```

Host-level tools (no port matching required):
```
nmap_scan, nmap, subfinder_enum, subfinder, subfinder_scan,
amass, amass_scan, dnsx, dnsx_scan, fierce, fierce_scan,
dnsenum, dnsenum_scan, nbtscan, rustscan, masscan, arp_scan,
responder, autorecon
```

URL-level tools (hostname + port must match):
```
nuclei_scan, nuclei, katana_crawl, katana, sqlmap_probe, sqlmap,
ffuf_discover, ffuf, gobuster_discover, gobuster,
nikto, dirb, httpx, dalfox, wpscan, feroxbuster
```

Unknown tools: URL-level rules applied (conservative default).

**2. Approval** — gated tools blocked until HITL approves:
```
sqlmap, sqlmap_probe, metasploit, exploit
```

**3. Budget** — atomic under threading.Lock:
```
tool_spent_usd + est_cost ≤ hard_cap ($5.00 default)
```
Reserves atomically. Refunds on failure.

**4. Pre-create audit record** — ToolInvocation written before execution.

**5. Cleanup registration** — expected artifact path logged in `_pending` dict.
`cleanup()` in finally block removes orphaned files on crash.

---

## 12. HexStrikeClient and the MCP Protocol

**File:** `blackbox_service/toolchannel/hexstrike_client.py`

### Two servers, two ports

| Server | Port | Purpose |
|--------|------|---------|
| `hexstrike_server.py` (Flask) | 8888 | Runs actual security tools (156 routes) |
| `hexstrike_mcp.py` (FastMCP) | 8001 | MCP JSON-RPC interface (151 `@mcp.tool()` functions) |

Our client talks to port 8001 (MCP). Flask is internal only.

### URL derivation

```python
def __init__(self, base_url: str, timeout_s: float = 300.0) -> None:
    self._base_url = base_url.rstrip("/")        # Flask :8888 (health check)
    self._mcp_url = base_url.rstrip("/").replace(":8888", ":8001")  # MCP :8001
```

### MCP JSON-RPC format

**Tool list:**
```json
POST http://hexstrike:8001/mcp
Content-Type: application/json
Accept: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

→ {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"nmap_scan","description":"...","inputSchema":{...}}, ...]}}
```

**Tool call:**
```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "nuclei_scan", "arguments": {"target": "https://example.com", "severity": "medium"}}}

→ {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[cve-2021-44228] ..."}]}}
```

### Why `json_response=True` and `stateless_http=True` are required

Without `json_response=True`: FastMCP returns SSE streams, requires `Accept: text/event-stream`.
Without `stateless_http=True`: FastMCP requires `Mcp-Session-Id` header (HTTP 400 without it).
Both must be True for our simple synchronous httpx client to work.

---

## 13. HexStrike Architecture

HexStrike runs in one Docker container with two processes:
```bash
# start.sh
python3 hexstrike_server.py &           # Flask :8888
# wait for health check (up to 30s)
python3 hexstrike_mcp.py \
    --transport streamable-http \
    --mcp-host 0.0.0.0 \
    --mcp-port 8001 &
```

Flask provides 156 routes like:
```
GET  /health        → tool availability map (153 tools checked via `which <name>`)
POST /api/tools/nmap     → runs: nmap -T4 --open {target}
POST /api/tools/nuclei   → runs: nuclei -u {target} -s medium -json
POST /api/tools/gobuster → runs: gobuster dir -u {url} -w {wordlist}
```

FastMCP wraps these as `@mcp.tool()` functions that call the Flask API internally.

---

## 14. EngagementOrchestrator

**File:** `blackbox_service/orchestrator.py`

### State machine

```
"created"
    ↓  start_engagement()
"running" / phase="discovery"
    ↓  DiscoveryAgent.run() (up to 20 steps)
"running" / phase="access_test"
    ↓  AccessTestAgent.run() (up to 20 steps)
    ↓  (if approval needed)
"paused_for_approval"
    ↓  approve(approved=True)
"running" / phase="confirm_evidence"
    ↓  ConfirmEvidenceAgent.run() (15 steps — hardcoded in approve())
"running" / phase="report"
    ↓  ExecutiveReport generated
"completed" / phase="done"
```

### Data passing between phases

```python
# Orchestrator passes DiscoveryAgent output to AccessTestAgent:
ctx_access = AgentContext(
    state={
        "discovery_endpoints": rec.attack_surface.endpoints,  # from DiscoveryAgent
        "available_tools": self._get_available_tools(),        # HexStrike tool schemas
    }
)

# Passes AccessTestAgent output to ConfirmEvidenceAgent:
ctx_confirm = AgentContext(
    state={
        "suspected_findings": [x.model_dump() for x in rec.suspected_findings],
        "available_tools": self._get_available_tools(),
    }
)
```

### Budget tracking — two separate pools

```
LLM budget (rec.budget.spent_usd):
  Incremented by orchestrator after each phase
  Thresholds: warn@80%, pause@95%, terminate@100%

Tool budget (rec.tool_spent_usd):
  Managed by SecurityToolGate under threading.Lock
  Hard cap: $5.00 (BLACKBOX_TOOL_BUDGET_HARD_CAP_USD)
  Refunded on tool failure
```

### Reachability cache (30s TTL)

```python
def _live_hexstrike_reachable(self) -> bool:
    if now - self._last_reachability_ts >= 30.0:
        self._hexstrike_reachable = self._hexstrike_client.health()
        if self._hexstrike_reachable:
            self._available_tools = self._hexstrike_client.list_tools()  # 151 tools
        self._last_reachability_ts = now
    return self._hexstrike_reachable
```

---

## 15. BlackboxService

**File:** `blackbox_service/service.py`

Manages the lower-level "run" abstraction: browser sessions, single-agent loops,
action execution. Used by both the technical dashboard AND the engagement pipeline.

### Planner hierarchy (single-agent / technical dashboard mode only)

```
build_planner():
├── Both Anthropic + Gemini keys → FailoverPlanner(Anthropic, Gemini)
│   Sticky: once it fails over to Gemini, stays on Gemini for the run
├── Anthropic only → AnthropicPlanner
├── Gemini only    → GeminiPlanner
└── Neither        → RuleBasedPlanner (4 fixed steps: console→network→eval_js→snapshot)
```

The engagement pipeline (agents_v2) does NOT use these planners. Each agent calls
`_call_llm()` directly in `AgentBase`.

---

## 16. Runtime Layer

**File:** `blackbox_service/runtime.py`

| Runtime | When used | Browser | Notes |
|---------|-----------|---------|-------|
| InMemoryRuntime | Testing, offline | None | Stores state in Python dicts; `eval_js` is safe AST evaluator |
| PlaywrightRuntime | Local dev with `USE_PLAYWRIGHT=true` | Real Chromium | Thread-unsafe directly |
| ThreadedPlaywrightRuntime | All production use | Real Chromium | Thread-safe via single owner thread + task queue |

ThreadedPlaywrightRuntime routes all calls through a queue to the Playwright owner thread,
since Playwright's sync API cannot be called from multiple threads.

---

## 17. SQLite Event Store

**File:** `blackbox_service/store.py`

Three tables: `runs`, `events` (append-only), `tabs`. Events are never updated or deleted
— they provide a complete audit trail and enable SSE replay for late-joining browsers.

Event types follow a hierarchy:
```
agent.thought / agent.hypothesis / agent.reasoning / agent.step.completed
action.navigate / action.click / action.fill / action.snapshot
observation.console / observation.network / observation.page_content
artifact.screenshot
agent.started / agent.finished / agent.failed
run.started / run.stopped
```

---

## 18. Event Bus and SSE Streaming

Two buses serve two different UI surfaces:

**RunEventBus** (`stream.py`) — for technical dashboard (`/runs/{id}/stream`)
- In-memory list per run
- Async generator with 50ms polling
- Simple append + yield

**EngagementEventBus** (`engagement_bus.py`) — for Ops Console (`/engagements/{id}/stream`)
- One `queue.Queue(maxsize=512)` per consumer
- Thread-safe fan-out (orchestrator threads publish; SSE coroutines consume)
- Drops messages on full queue (slow consumer protection)
- SSE endpoint replays full history on connect, then polls live queue

---

## 19. API Layer

**File:** `blackbox_service/api.py`

All routes registered in `create_app()` factory. HTML for both dashboards served inline
as f-string templates.

**Health:** `GET /health`, `GET /config/models`  
**Runs (single-agent):** `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/actions`, `GET /runs/{id}/stream`, etc.  
**Engagements (pipeline):** `POST /engagements`, `POST /engagements/{id}/start`, `POST /engagements/{id}/approval`, `GET /engagements/{id}/stream`, etc.  
**UI:** `GET /dashboard`, `GET /engagement-dashboard`, `GET /ops-console`, `GET /static/*`, `GET /artifacts/{run_id}/{filename}`

---

## 20. Ops Console Frontend

**Files:** `blackbox_service/static/ops_console.{html,js,css}`

Real-time SSE-driven UI. Key behaviours:

- Connects to `GET /engagements/{id}/stream` on Start click
- Tool and LLM badges auto-refresh every 30 seconds via `setInterval(refreshCapabilities, 30000)`
- If HexStrike comes online after browser is already open, badge updates within 30s (no reload needed)
- `maxStepsInput` field (default 20) — user can set per engagement
- Approve/Reject buttons appear when engagement status = `"paused_for_approval"`

---

## 21. Settings and Configuration

**File:** `blackbox_service/settings.py`

API keys loaded from `.env` file ONLY (never from shell environment — security design).

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `ANTHROPIC_API_KEY` | (required) | Claude API — loaded from .env only |
| `GEMINI_API_KEY` | (optional) | Gemini fallback — loaded from .env only |
| `BLACKBOX_AGENT_MODEL` | `claude-sonnet-4-6` | LLM for all agents |
| `BLACKBOX_GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model |
| `BLACKBOX_HOST` | `127.0.0.1` | FastAPI bind host |
| `BLACKBOX_PORT` | `8080` | FastAPI bind port |
| `BLACKBOX_USE_PLAYWRIGHT` | `true` | Real browser (false = InMemory) |
| `BLACKBOX_BROWSER_HEADLESS` | `false` | Headless mode (true in Docker) |
| `BLACKBOX_TARGET_URL` | `http://localhost:3000` | Default target in dashboard |
| `BLACKBOX_AGENT_MAX_STEPS` | `20` | Steps per agent |
| `BLACKBOX_AGENT_STEP_DELAY_MS` | `1000` | Delay between steps |
| `BLACKBOX_HEXSTRIKE_ENABLED` | `true` | Enable HexStrike (graceful degradation if down) |
| `BLACKBOX_HEXSTRIKE_URL` | `http://localhost:8888` | HexStrike Flask URL |
| `BLACKBOX_HEXSTRIKE_TIMEOUT_S` | `300` | Max seconds per tool call |
| `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` | `5.0` | Tool-only spending cap |

---

## 22. Budget System

Two completely independent pools:

**LLM budget** — tracks AI reasoning costs (estimated), enforces engagement-level limit.
Thresholds at 80% (warn), 95% (pause), 100% (terminate).

**Tool budget** — tracks HexStrike tool costs (estimated per `_TOOL_COST_MAP`).
Hard cap, atomically enforced, refunded on tool failure. Separate because tool costs
should not eat into the AI reasoning budget.

---

## 23. HITL Approval Gate

**When it triggers:**
- `approval_mode="mandatory"` → always pauses after AccessTest
- `approval_mode="optional"` (default) → pauses only if findings exist AND not yet approved
- `approval_mode="none"` → never pauses

**What changes on approval:**
1. `rec.approval_granted = True` → SecurityToolGate unlocks sqlmap, metasploit, exploit
2. New thread starts ConfirmEvidenceAgent with 15 steps (hardcoded)
3. Severity cap lifted — confirmed findings can be high/critical

**What changes on rejection:**
1. Status → "completed" without confirmation phase
2. Report generated from suspected (unconfirmed) findings only

---

## 24. Dynamic Tool Discovery

```
Orchestrator starts
  → _live_hexstrike_reachable() (every 30s TTL)
     → HexStrikeClient.list_tools()
        → POST http://hexstrike:8001/mcp {"method": "tools/list"}
        → Returns 151 tool schemas with names, descriptions, inputSchema
     → Stored in orchestrator._available_tools
  → Injected into AgentContext.state["available_tools"] for all 3 agents
  → Agents build allowed_actions = tool_names + base_BIE_actions
  → LLM sees ALL 151 tools in context
  → LLM chooses any tool
  → AgentBase._effective_tool_names = hardcoded | dynamic (union)
  → Routes to SecurityToolGate if in set
  → SecurityToolGate enforces scope/approval/budget
  → HexStrikeClient sends MCP tools/call
  → HexStrike Flask executes actual binary
```

**Fallback** (HexStrike offline): list_tools() → GET /health → bare tool names (no schemas).
LLM uses training knowledge for parameters.

---

## 25. Remaining Bottlenecks

This section is completely honest about what still limits the system.

### B1 — LLM only sees last 6 observations (SIGNIFICANT)

```python
"recent_observations": [... for o in observations[-6:]]
```

On step 15, the LLM cannot see what happened on steps 1-9. It may re-probe paths it
already tested. `tools_already_called` compensates by showing all tools used across all
steps, but specific response content from early steps is gone.

**Impact:** For long engagements (20+ steps), the LLM loses context. It may:
- Re-test endpoints with the same payloads
- Forget that a specific path returned interesting data
- Miss connections between early discovery and late exploitation opportunities

**Fix complexity:** Medium. Solution would be to summarize early observations into a
"memory" field that persists across steps. This is a known LLM agent architecture problem.

### B2 — Confirm phase hardcoded to 15 steps (MODERATE)

When a human approves, `approve()` hardcodes 15 steps regardless of how many findings exist:
```python
self.start_engagement(engagement_id, max_steps_per_agent=15, step_delay_ms=200)
```

If there are 10 findings each needing 2 steps (re-test + snapshot) = 20 steps needed.
15 steps means the last 2-3 findings may not be fully confirmed.

**Fix complexity:** Low. Pass the original `max_steps_per_agent` from the engagement record,
or derive from `len(suspected_findings) × 2 + 3 buffer`.

### B3 — Auto-detection is path-dependent (MINOR)

The automatic pattern detection (admin/API/IDOR) only triggers when specific URL patterns
are present (`/admin`, `/api`, numeric ID). Applications that use different URL structures
(e.g., `/panel`, `/console`, `/manage`, `/v2/resources/abc123`) are not auto-detected.

The LLM can still find these — auto-detection is supplementary, not the primary mechanism.

### B4 — HexStrike tool parameters are LLM-guessed (MINOR)

When HexStrike's MCP server provides tool schemas (e.g., gobuster needs `url` not `target`,
subfinder needs `domain` not `target`), these are shown to the LLM. But the LLM must still
form the right parameter structure. If it guesses wrong, the scope check may reject the call
or the tool may run incorrectly.

**Current mitigation:** `_extract_scope_target(params)` checks multiple key names (target, url,
domain, host, site). Scope check now works even with different param names.

### B5 — Page content truncated at 4000 chars (MINOR)

```python
"text": document.body.innerText.slice(0, 4000)
```

Large pages (SPA dashboards, data-heavy responses) are truncated. The LLM may miss
content that appears after the first ~3000 words. The first 4000 chars is usually the most
interesting (page structure, menus, forms), but verbose API responses with large data sets
may be cut mid-JSON.

### B6 — Single-turn LLM calls (BY DESIGN, but worth knowing)

Each step is one independent LLM call. There is no conversation history between steps.
The LLM cannot "remember" a finding from step 2 when making a decision on step 8 unless:
- It appears in `recent_observations` (last 6 only)
- It appears in `tools_already_called` (name only, no result)
- It was added to `suspected_findings` by auto-detection (passed back via state)

This is fundamentally different from a conversational AI assistant that builds memory across turns.

### B7 — Budget estimation is not actual cost (INFORMATIONAL)

`_TOOL_COST_MAP` values are rough estimates for budget tracking, not real API charges.
The $0.05 for nuclei_scan is an estimate — actual HexStrike running costs are zero
(you host it yourself). The LLM token costs are also estimates.

### B8 — nuclei templates must be pre-downloaded (OPERATIONAL)

`nuclei -update-templates` runs during Docker build. If nuclei templates are outdated
(new CVEs published after build), they won't be detected until the container is rebuilt.
The Dockerfile includes `nuclei -update-templates -silent || true` on build.

### What is NOT a bottleneck (addressed concerns)

| Was a concern | Current state | Fixed by |
|--------------|---------------|---------|
| max_tokens 1024 | Now **2048** | `base.py` update |
| 5-goal hardcoded vuln list | Now **open-ended categories** | System prompt rewrite |
| 4 hardcoded tools only | Now **151 dynamic tools** | Dynamic tool discovery |
| www.example.com scope mismatch | **Fixed** | `_hosts_equivalent()` normalization |
| Tool params wrong key (url vs target) | **Fixed** | `_extract_scope_target()` multi-key |
| Approve() 8 steps too few | Now **15 steps** | `orchestrator.py` update |
| Report capped at 8 findings | Now **20 findings** | `orchestrator.py` update |
| HexStrike badge stale on page | **Auto-refreshes 30s** | `setInterval` in JS |
| MCP 406 / 400 errors | **Fixed** | `json_response+stateless_http=True` |
| amass/dnsx/rustscan out_of_scope | **Fixed** | `_HOST_LEVEL_TOOLS` expansion |

---

## 26. Phase A vs Phase B

| Aspect | Phase A (`blackbox_service/`) | Phase B (`agents/`) |
|--------|-------------------------------|---------------------|
| Architecture | FastAPI service + SQLite + HexStrike | Standalone script |
| Agents | 3 specialized in sequence | 1 general-purpose |
| Tools | 151 HexStrike tools | None |
| State | SQLite (persistent) | In-memory only |
| UI | Ops Console (SSE real-time) | Injected browser sidebar |
| HITL | Approval gate before confirm | None |
| Budget | LLM + tool tracking | None |
| Findings | Structured + confirmed | Text output only |
| Report | ExecutiveReport with severities | None |
| Cross-imports | Zero (verified by grep) | Zero |

---

## 27. Testing Architecture

110 tests in `tests/`. Key files:

| File | Tests |
|------|-------|
| `test_security_gate.py` | Scope math, www normalization, approval, budget atomicity |
| `test_hexstrike_client.py` | MCP request/response format, timeout, fallback |
| `test_discovery_tools.py` | nmap/katana/subfinder/nuclei output parsing |
| `test_access_test_tools.py` | nuclei→SuspectedFinding, severity capping, keyword detection |
| `test_confirm_tools.py` | sqlmap→ConfirmedFinding, false positive, evidence tagging |
| `test_engagement_api.py` | Full engagement lifecycle via HTTP |
| `test_approval_resume.py` | Approval gate → ConfirmEvidence transition |
| `test_toolchannel_integration.py` | Full SecurityToolGate→HexStrikeClient path |
| `test_agent_reasoning.py` | ScriptedPlanner + BlackboxService (Phase A single-agent) |

Run: `uv run pytest tests/ -q --ignore=tests/test_settings.py` → 110 passed.

---

## 28. End-to-End Flow — Complete Example

**Scenario:** Testing OWASP Juice Shop at `http://juice-shop.local:3000`

### Startup

```bash
make tools
# → clones hexstrike/ if absent
# → docker compose --profile tools up --build
# juice-shop on :3000, hexstrike on :8888+:8001, blackbox-agent on :8080

uv run blackbox-agent
# → loads .env (ANTHROPIC_API_KEY, model=claude-sonnet-4-6)
# → HexStrikeClient health check → reachable=True
# → list_tools() → 151 tools cached
# → FastAPI starts on :8080
```

### Create engagement

```
POST /engagements
{"target_url": "http://juice-shop.local:3000", "budget_usd": 10, "approval_mode": "optional"}

→ _normalize_engagement_url() → "http://juice-shop.local:3000" (already has scheme)
→ EngagementRecord created: eng-3f8a2b1c4d5e
```

### Start engagement

```
POST /engagements/eng-3f8a2b1c4d5e/start
{"max_steps_per_agent": 20, "step_delay_ms": 200}

→ background thread spawned
→ SecurityToolGate created (reachable=True, tool_cap=$5.00)
→ Playwright: new_context() + open_tab("http://juice-shop.local:3000")
→ run_id: "run-ca1a270f9302"
```

### Discovery phase (steps 1-20)

```
Step 1: LLM → nmap_scan {"target": "juice-shop.local"}
  Gate: scope=juice-shop.local matches ✓, not gated ✓, $0+$0.02≤$5 ✓
  → MCP: tools/call nmap_scan → hexstrike_server: nmap -T4 --open juice-shop.local
  → Result: "3000/tcp open http Node.js"
  → code: tech_stack.add("Node.js")
  → SSE: tool.invoked, agent.step → Ops Console shows step

Step 2: LLM → katana_crawl {"url": "http://juice-shop.local:3000", "depth": 3}
  Gate: URL-level scope: "juice-shop.local:3000" matches ✓
  → Result: 47 URLs including /api/users, /#/login, /#/basket
  → code: endpoints.append(47 items)

Step 3-15: LLM alternates http_get on sensitive paths + subfinder + nuclei
  → nuclei finds: missing-csp (medium), jwt-weak-secret (high→capped medium)
  → http_get /api: extracts /api/v1, /api/products path hints from JS

DiscoveryAgent.summarize() → 23 endpoints, hosts=["juice-shop.local"], tech_stack=["Node.js"]
```

### Access test phase (steps 1-20)

```
login_candidates: [{"url": "http://juice-shop.local:3000/#/login"}]
api_candidates: [{"url": "...api/users"}, {"url": "...api/products"}, ...]

Step 1: LLM → navigate + get_page_content on /#/login
  → Angular SPA renders, finds email+password inputs

Step 2: LLM → ai_navigate to log in with admin@juice-sh.op/admin123
  → Tier 4: browser-use Agent completes login, returns JWT
  → auth_status = "success"

Step 3: LLM → http_get /api/users (no auth headers)
  → 200 response, body: [{"id":1,"email":"admin@...","role":"admin",...}]
  → code auto-detect: "/api" + "email" in body → SuspectedFinding(missing_auth_api, medium)

Step 4: LLM → http_get /api/orders/1
  → 200 response, body: {"id":1,"products":[...],"UserId":1}
  → code auto-detect: numeric ID + "id" in body → SuspectedFinding(idor, high)

Step 5-8: LLM tests XSS, SQLi, path traversal, CORS
  → LLM constructs payloads: <script>alert(1)</script>, ' OR '1'='1, ../../../etc/passwd
  → Some fail (app validates input) → LLM logs "not vulnerable"
  → XSS: reflected in search results → SuspectedFinding(xss, high, confidence=8)

Step 9: LLM → gobuster_scan {"url": "http://juice-shop.local:3000", "mode": "dir"}
  → finds /ftp/ (200), /security.txt (200)

AccessTestAgent.summarize():
  suspected_findings: [missing_auth_api, idor, xss]
  auth_status: "success"
```

### Approval gate

```
approval_mode="optional" + 3 findings + not approved → paused_for_approval
SSE event → Ops Console: "WAITING FOR APPROVAL — 3 suspected finding(s)"
Approve/Reject buttons active
```

### Human approves

```
POST /engagements/eng-3f8a2b1c4d5e/approval {"approved": true}

→ approval_granted = True (sqlmap now allowed)
→ new thread: ConfirmEvidenceAgent (15 steps hardcoded)
```

### Confirm phase (steps 1-15)

```
Step 1: LLM → http_get /api/users (re-test), hypothesis: "confirm:sf-a1b2c3"
  → 200 + email list → confirm_ok["sf-a1b2c3"] = True

Step 2: LLM → snapshot, hypothesis: "evidence:sf-a1b2c3"
  → screenshot saved
  → confirm_ok=True → ConfirmedFinding(missing_auth_api, confidence=8, status="confirmed")

Step 3: LLM → sqlmap_probe {"target": "http://juice-shop.local:3000/api/users?id=1"}
  hypothesis: "sqlmap_confirm:sf-d4e5f6"
  Gate: approval_granted=True ✓ → executes
  → sqlmap finds injectable parameter
  → ConfirmedFinding(sql_injection, confidence=10, "confirmed by sqlmap")

Step 4-6: LLM retests XSS, IDOR
  → XSS confirms: reflected payload executed → ConfirmedFinding
  → IDOR confirms: /api/orders/2 returns different user data → ConfirmedFinding

Steps 7-15: Remaining suspected findings tested
  → Some confirmed, some false_positive
```

### Report

```
_build_report():
  confirmed_findings: 4 (missing_auth_api + sql_injection + xss + idor)
  false_positives: 0
  ExecutiveReport:
    summary: "4 confirmed vulnerabilities (0 critical, 2 high, 2 medium, 0 low)"
    findings_overview: {"high": 2, "medium": 2}
    key_risks: ["SQL Injection via api/users", "XSS in search", ...]
    recommendations: [...]

rec.status = "completed"
SSE → Ops Console: "DONE ✓ — 4 confirmed finding(s)"
"View Report" button appears
```

---

## Glossary

| Term | Definition |
|------|-----------|
| AgentBase | Abstract base class — all 3 agents inherit the same loop from here |
| BIE | Browser Interaction Engine — routes to HTTP, Playwright, or AI navigation |
| EngagementOrchestrator | Manages the full lifecycle: state machine, phases, budget, events |
| BlackboxService | Underlying service: runs, tabs, single-agent loop, action execution |
| HITL | Human In The Loop — the approval gate before destructive confirmation |
| MCP | Model Context Protocol — JSON-RPC standard used to communicate with HexStrike |
| SSE | Server-Sent Events — one-way HTTP streaming for real-time UI updates |
| SuspectedFinding | Potential vulnerability from AccessTest — unconfirmed |
| ConfirmedFinding | Reproduced vulnerability from ConfirmEvidence — with evidence |
| SecurityToolGate | Mandatory policy layer between agents and HexStrike (scope, approval, budget) |
| ToolChannel | Directory containing SecurityToolGate + HexStrikeClient |
| available_tools | 151 HexStrike tool schemas fetched at engagement start |
| effective_tool_names | Union of hardcoded + dynamic tool names in AgentBase |
| step_sink | Callback from agents → SSE events → Ops Console |
| Phase A | Enterprise engagement service (`blackbox_service/`) — this codebase |
| Phase B | Standalone browser demo agent (`agents/`) — separate branch |
| `confirm_ok` | Dict tracking which suspected findings were re-confirmed by ConfirmEvidenceAgent |
| tools_already_called | Counter dict in LLM context preventing tool repetition across all steps |
