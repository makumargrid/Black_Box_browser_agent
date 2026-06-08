# Blackbox Agent — Complete Technical Reference

> Everything you need to know about every component, every interaction, and every flow.
> Detailed enough to answer any question about the codebase.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Map](#2-architecture-map)
3. [The Agent Loop — AgentBase](#3-the-agent-loop--agentbase)
4. [DiscoveryAgent](#4-discoveryagent)
5. [AccessTestAgent](#5-accesstestagent)
6. [ConfirmEvidenceAgent](#6-confirmevidenceagent)
7. [Browser Interaction Engine (BIE)](#7-browser-interaction-engine-bie)
8. [SecurityToolGate](#8-securitytoolgate)
9. [HexStrikeClient and the MCP Protocol](#9-hexstrikeclient-and-the-mcp-protocol)
10. [HexStrike Architecture](#10-hexstrike-architecture)
11. [EngagementOrchestrator](#11-engagementorchestrator)
12. [BlackboxService](#12-blackboxservice)
13. [Runtime Layer](#13-runtime-layer)
14. [SQLite Event Store](#14-sqlite-event-store)
15. [Event Bus and SSE Streaming](#15-event-bus-and-sse-streaming)
16. [API Layer](#16-api-layer)
17. [Ops Console Frontend](#17-ops-console-frontend)
18. [Settings and Configuration](#18-settings-and-configuration)
19. [Budget System](#19-budget-system)
20. [HITL Approval Gate](#20-hitl-approval-gate)
21. [Dynamic Tool Discovery](#21-dynamic-tool-discovery)
22. [Phase A vs Phase B](#22-phase-a-vs-phase-b)
23. [Testing Architecture](#23-testing-architecture)
24. [End-to-End Flow — Complete Example](#24-end-to-end-flow--complete-example)

---

## 1. System Overview

The Blackbox Agent is an **autonomous security engagement platform**. Given a target URL, it
uses AI-driven agents to perform black-box penetration testing — mapping the attack surface,
testing for vulnerabilities, and producing a structured report — without any prior knowledge of
the target's internals (no source code, no credentials, no documentation).

### What "blackbox" means

The agent can only interact with the target the same way an attacker would: through HTTP
requests, browser interaction, and the responses it receives. It cannot read source code or
database contents directly. Everything it learns comes from observable behaviour.

### Two execution paths

**Phase A — Engagement Service** (`blackbox_service/`)

A full enterprise-grade security engagement platform running as a FastAPI HTTP server.
Three specialized AI agents run in sequence. HexStrike provides 150+ real security tools.
A human-in-the-loop approval gate controls destructive testing. An SSE event bus streams
every action and finding to the Ops Console in real time. Results are stored in SQLite.

Use this when: you want structured engagements, human oversight, budget control, real tools.

**Phase B — Standalone Browser Agent** (`agents/`, `run_agent.py`)

A standalone script that drives a real browser using the `browser-use` library with
Claude or Gemini as the LLM. No orchestrator, no HexStrike, no database. The agent
injects an overlay sidebar into the live browser so you can watch it think in real time.

Use this when: you want a quick single-agent demo against a local target.

---

## 2. Architecture Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Phase A Service                              │
│                                                                     │
│  Browser/CLI ──── FastAPI (api.py) ──── EngagementOrchestrator     │
│       │                │                       │                    │
│       │           BlackboxService          [Agent Pipeline]         │
│       │                │                   ↓         ↓         ↓   │
│       │           ┌────┴────┐         Discovery  AccessTest  Confirm│
│    SSE/HTTP  SQLiteStore  Runtime          │         │         │    │
│       │           │       (Playwright/     └─────────┴─────────┘    │
│  Ops Console   Events  InMemory)                    │               │
│  (browser)         │                          AgentBase.run()       │
│                EventBus ←──────── SSE ──────── step_sink            │
│                    │                                │               │
│                    └──────────────┐           BIE / SecurityGate    │
│                                  │                 │                │
│              ┌───────────────────┘           HexStrikeClient        │
│              │                                     │                │
│         HexStrike (Docker)                   MCP JSON-RPC          │
│         ┌──────────────┐               POST :8001/mcp              │
│         │ Flask :8888  │                     │                      │
│         │ FastMCP :8001│ ←───────────────────┘                      │
│         │ 151 tools    │                                            │
│         └──────────────┘                                            │
└─────────────────────────────────────────────────────────────────────┘
```

### Component relationships

| Component | File | Depends on |
|-----------|------|------------|
| FastAPI app | `api.py` | BlackboxService, EngagementOrchestrator |
| EngagementOrchestrator | `orchestrator.py` | BlackboxService, BIE, HexStrikeClient |
| BlackboxService | `service.py` | SQLiteEventStore, Runtime, RunEventBus, Planners |
| AgentBase | `agents_v2/base.py` | BIE, SecurityToolGate |
| BIE | `bie/engine.py` | BlackboxService.execute_action |
| SecurityToolGate | `toolchannel/security_gate.py` | HexStrikeClient |
| HexStrikeClient | `toolchannel/hexstrike_client.py` | HexStrike Docker service |
| Runtime | `runtime.py` | Playwright (optional) |
| Store | `store.py` | SQLite |

---

## 3. The Agent Loop — AgentBase

**File:** `blackbox_service/agents_v2/base.py`

Every agent (Discovery, AccessTest, ConfirmEvidence) inherits from `AgentBase`. The loop
is the same for all three — only `plan_next()` and `_after_observation()` differ.

### The loop step by step

```python
def run(ctx: AgentContext) -> dict:
    local_state = self.initialize_state(ctx)      # agent-specific initial state
    observations = []
    effective_tool_names = self._effective_tool_names(ctx)  # hardcoded + HexStrike tools

    for _ in range(ctx.max_steps):               # hard cap, default 20
        step = self.plan_next(ctx, local_state, observations)  # LLM CALL
        if step.done:
            break

        if step.action_type in effective_tool_names:
            result = self._invoke_tool(step.action_type, step.params)  # → Gate → HexStrike
        else:
            result = self._bie.request(BIERequest(...))                # → http_get / Playwright

        observations.append({
            "action_type": step.action_type,
            "ok": result.ok,
            "result": result.result,
            "stdout": result.stdout,
            "error": result.error,
            "cost_usd": result.cost_usd,
            "note": step.note,
        })

        self._after_observation(local_state, observations[-1])  # update local state
        time.sleep(ctx.step_delay_ms / 1000)                    # throttle
        self._step_sink("agent.step", {...})                     # → SSE → Ops Console

    return self.summarize(ctx, local_state, observations)
```

### AgentContext — what every agent knows

```python
@dataclass
class AgentContext:
    engagement_id: str        # which engagement this run belongs to
    run_id:        str        # the Playwright/InMemory browser session
    target_url:    str        # always normalized, e.g. "https://blogger.com"
    max_steps:     int = 20   # hard cap
    step_delay_ms: int = 200  # pause between steps (rate limiting)
    state:         dict       # agent-specific: discovery_endpoints, available_tools, etc.
    anthropic_api_key: str    # passed from .env
    anthropic_model:   str    # e.g. "claude-sonnet-4-6"
```

### How the LLM decides — `_call_llm()`

Every call to `plan_next()` ends with a call to `_call_llm()`. This method:

1. **Checks for API key** — if missing, returns `{done: True, thought: "No API key"}` immediately.

2. **Builds the payload:**
   ```python
   payload = {
       "model": ctx.anthropic_model,
       "max_tokens": 1024,
       "system": system_prompt,        # agent-specific instructions
       "messages": [{"role": "user", "content": json.dumps(user_context)}]
   }
   ```

3. **Calls Anthropic Messages API directly** (raw httpx, no SDK):
   ```
   POST https://api.anthropic.com/v1/messages
   x-api-key: <from .env>
   anthropic-version: 2023-06-01
   ```

4. **Extracts the first JSON object** from the response text using `re.search(r"\{.*\}", text, re.DOTALL)`.
   The LLM always returns a JSON block like:
   ```json
   {
       "thought": "I should scan open ports first",
       "hypothesis": "Target may expose non-standard services",
       "action_type": "nmap_scan",
       "params": {"target": "example.com", "profile": "quick"},
       "done": false
   }
   ```

5. **Falls back gracefully** — if JSON parsing fails, returns `{done: True}` so the agent
   terminates cleanly rather than crashing.

### AgentStep — what the LLM decision becomes

```python
@dataclass
class AgentStep:
    done:        bool           # should the loop terminate?
    goal:        str = ""       # the LLM's "thought" — why it's doing this
    action_type: str = "none"   # what to execute
    params:      dict = {}      # parameters for the action
    note:        str = ""       # the LLM's "hypothesis" — used for finding linkage
```

### Routing: tools vs BIE

After `plan_next()` returns an `AgentStep`, the loop checks:

```python
effective_tool_names = self._effective_tool_names(ctx)
# = hardcoded frozenset (e.g. {"nmap_scan", "nuclei_scan"})
# UNION dynamic HexStrike tools from ctx.state["available_tools"]
```

If `action_type` is in that set → routes to `SecurityToolGate` → `HexStrikeClient` → HexStrike.
If not → routes to `BIE` → http_get / Playwright / browser-use depending on tier.

### `_effective_tool_names()` — dynamic expansion

```python
def _effective_tool_names(self, ctx: AgentContext) -> frozenset[str]:
    dynamic = frozenset(
        t["name"] for t in ctx.state.get("available_tools", [])
        if isinstance(t, dict) and t.get("name")
    )
    return self._TOOL_ACTION_NAMES | dynamic
```

When HexStrike is online, `ctx.state["available_tools"]` contains all 151 tool schemas,
so `effective_tool_names` expands from 1-4 hardcoded names to 151+ names. Any tool the
LLM names will be routed through the SecurityToolGate.

---

## 4. DiscoveryAgent

**File:** `blackbox_service/agents_v2/discovery.py`  
**Purpose:** Map the attack surface — every page, endpoint, port, subdomain, and technology.  
**Produces:** `endpoints[]`, `hosts[]`, `tech_stack[]`, `nuclei_findings[]`

### State initialization

```python
def initialize_state(self, ctx):
    return {
        "endpoints": [],          # all discovered URLs with status codes
        "seen_urls": [],          # dedup set for crawl
        "hosts": [urlparse(ctx.target_url).netloc],  # starts with just the target
        "total_cost_usd": 0.0,
    }
```

### What the LLM receives per step

```json
{
    "target_url": "https://juice-shop.local:3000",
    "step": 3,
    "max_steps": 20,
    "endpoints_found": 7,
    "tools_enabled": true,
    "tools_already_called": {"nmap_scan": 1, "katana_crawl": 1},
    "available_tools": [{"name": "subfinder_enum", "description": "..."}, ...],
    "recent_observations": [
        {"action_type": "nmap_scan", "ok": true, "result_preview": "80/tcp open http\n3000/tcp open..."},
        {"action_type": "katana_crawl", "ok": true, "result_preview": "http://juice-shop.local:3000/api\n..."}
    ],
    "allowed_actions": ["subfinder_enum", "nuclei_scan", "gobuster_scan", ..., "http_get", "navigate"]
}
```

### `_after_observation()` — what it does with results

The agent maintains local state across all steps. After each observation:

**From `nmap_scan`:**
```
nmap output: "3000/tcp open http Node.js"
→ local_state["tech_stack"].add("Node.js")
→ local_state["hosts"].append("10.0.0.1")  # from raw dict
```

**From `subfinder_enum`:**
```
subfinder output: "api.juice-shop.local\nstatic.juice-shop.local"
→ local_state["hosts"].append("api.juice-shop.local")
→ local_state["hosts"].append("static.juice-shop.local")
```

**From `katana_crawl`:**
```
katana output lines: "http://juice-shop.local:3000/api/users\nhttp://juice-shop.local:3000/login"
→ local_state["endpoints"].append({"url": "http://juice-shop.local:3000/api/users", "source": "katana"})
```

**From `nuclei_scan`:**
```
nuclei JSON: [{"template_id": "cve-2021-44228", "severity": "critical", "matched_at": "..."}]
→ local_state["nuclei_findings"].append({...})
# Note: nuclei findings go to nuclei_findings, NOT endpoints
# They are passed to AccessTestAgent for targeted exploitation
```

**From `http_get`:**
```python
# Extracts links from HTML body via regex
for m in _LINK_RE.finditer(body_preview):   # href="..." patterns
    candidate = urljoin(target_url, m.group(1))
    seen_urls.append(candidate)

# Extracts API path hints from JavaScript
for m in _PATH_RE.finditer(body_preview):   # /api/... patterns
    endpoints.append({"url": m.group(1), "source": "js-path-hint"})

# Extracts server technology
server = headers.get("server", "")
if server:
    tech_stack.add(server)  # e.g. "nginx/1.18.0"
```

### Output structure

```python
{
    "hosts": ["juice-shop.local", "api.juice-shop.local"],
    "endpoints": [
        {"url": "http://juice-shop.local:3000/api/users", "method": "GET",
         "status_code": 401, "auth_required": True, "source": "katana"},
        {"url": "http://juice-shop.local:3000/login", "method": "GET",
         "status_code": 200, "auth_required": False, "source": "discovery-http"},
        {"url": "/api/v1/products", "method": "GET", "status_code": 200,
         "auth_required": False, "source": "js-path-hint"},
    ],
    "tech_stack": ["Node.js", "nginx/1.18.0", "Express"],
    "nuclei_findings": [
        {"template_id": "missing-csp", "severity": "medium", "matched_at": "http://..."}
    ],
    "observation_count": 15,
    "cost_usd": 0.23
}
```

This entire dict is passed to the orchestrator which stores it in `EngagementRecord.attack_surface`
and passes `endpoints` to AccessTestAgent via `ctx.state["discovery_endpoints"]`.

---

## 5. AccessTestAgent

**File:** `blackbox_service/agents_v2/access_test.py`  
**Purpose:** Test for every class of vulnerability using full security knowledge.  
**Produces:** `SuspectedFinding[]` — potential vulnerabilities with evidence snippets.

### State initialization

```python
def initialize_state(self, ctx):
    endpoints = list(ctx.state.get("discovery_endpoints", []))
    login_candidates = [e for e in endpoints if "login" in e.get("url", "").lower()]
    api_candidates   = [e for e in endpoints if "/api" in e.get("url", "")]
    return {
        "login_candidates": login_candidates,  # pre-filtered for auth testing
        "api_candidates": api_candidates,      # pre-filtered for API testing
        "probe_index": 0,
        "stage": "auth",
        "tier4_attempted": False,
        "suspected": [],                       # SuspectedFinding objects accumulate here
        "total_cost_usd": 0.0,
        "auth_status": "not_attempted",
    }
```

### Open-ended vulnerability testing

The system prompt tells the LLM to test ALL vulnerability classes based on what Discovery
found. It includes a context-aware prioritization guide:

```
- Login forms exist → test authentication (default creds, SQLi, bypass)
- Numeric IDs in URLs → test IDOR
- File upload functionality → test unrestricted upload and path traversal
- XML/JSON processing → test XXE and injection
- Redirect parameters → test open redirect and SSRF
- JavaScript-heavy SPA → test DOM XSS and client-side logic
```

Example: if Discovery found `/api/products?id=1`, the LLM might test:
- `GET /api/products?id=2` (IDOR)
- `GET /api/products?id=-1` (negative ID handling)
- `GET /api/products?id=1' OR '1'='1` (SQLi)
- `GET /api/products?id=1&id=999` (parameter pollution)

### Automatic pattern detection in `_after_observation()`

Beyond what the LLM reports explicitly, the agent's code also scans HTTP responses
automatically for vulnerability patterns:

**Admin exposure detection:**
```python
if "/admin" in url and status_code == 200:
    has_admin_content = any(kw in body_lower for kw in
        ["dashboard", "users", "settings", "configuration",
         "manage", "panel", "admin panel", "system", "analytics"])
    if has_admin_content and not is_login_page and not is_redirect_page:
        self._add_suspected(
            vuln_type="broken_access_control",
            title="Admin route reachable without strict controls",
            endpoint=url,
            severity="high",
            confidence=7,
        )
```

**API data exposure detection:**
```python
if "/api" in url and status_code == 200:
    has_sensitive_data = any(kw in body_lower for kw in
        ["password", "secret", "token", "email", "ssn",
         "credit_card", "private", "internal", "user_id", "session"])
    if has_sensitive_data and not is_login_page:
        self._add_suspected(
            vuln_type="missing_auth_api",
            title="API endpoint exposes sensitive data without auth",
            endpoint=url,
            severity="medium",
            confidence=5,
        )
```

**IDOR auto-detection:**
```python
id_match = _ID_RE.search(url)   # finds the first numeric ID in the URL
if id_match and status_code == 200:
    has_record_data = any(kw in body_lower for kw in
        ["username", "email", "name", "address", "phone", "account", "profile"])
    if has_record_data and not is_login_page:
        value = id_match.group(1)
        next_id = str(int(value) + 1)
        alt_url = url.replace(value, next_id, 1)  # e.g. /users/1 → /users/2
        self._add_suspected(
            vuln_type="idor",
            title="Potential IDOR via numeric identifier",
            endpoint=alt_url,  # suggest the probed alternate URL
            severity="high",
            confidence=6,
        )
```

This dual approach (LLM + code) means finding detection happens at two levels:
the LLM's semantic understanding of responses AND deterministic code pattern matching.

### SuspectedFinding deduplication

Every finding gets a SHA-1 ID from `hash(vuln_type + endpoint)`:
```python
key = f"{vuln_type}|{endpoint}".encode("utf-8")
finding_id = f"sf-{hashlib.sha1(key).hexdigest()[:10]}"
if any(x.finding_id == finding_id for x in findings):
    return  # silently drop duplicate
```

This prevents the same vulnerability on the same endpoint from being reported twice, even if
both the LLM and the auto-detection code find it independently.

### Severity cap pre-approval

Before the HITL approval gate, all findings from `nuclei_scan` are capped at `medium`:
```python
def _cap_severity_pre_approval(severity: str) -> str:
    if _SEVERITY_ORDER.get(severity, 0) > _SEVERITY_ORDER["medium"]:  # critical=4, high=3
        return "medium"
    return severity
```

**Why?** If an attacker tool like nuclei reports a critical CVE, we want a human to review
it before the agent starts running sqlmap or other destructive confirmation tools. The severity
is escalated ONLY after a human clicks "Approve" in the Ops Console.

### Output structure

```python
{
    "auth_status": "failed",          # "success", "failed", "not_attempted"
    "suspected_findings": [
        {
            "finding_id": "sf-3a4f8b2c1d",
            "vuln_type": "idor",
            "title": "Potential IDOR via numeric identifier",
            "endpoint": "https://juice-shop.local:3000/api/users/2",
            "method": "GET",
            "severity": "high",   # capped at medium if from nuclei
            "confidence": 6,      # 1-10 scale
            "evidence_snippet": "numeric ID path with user data: {\"email\":\"user@...\"}",
            "source_agent": "access_test"
        },
        {
            "finding_id": "sf-8c9d2a1b3e",
            "vuln_type": "missing_auth_api",
            "title": "API endpoint exposes sensitive data without auth",
            "endpoint": "https://juice-shop.local:3000/api/users/whoami",
            "severity": "medium",
            "confidence": 5,
            "evidence_snippet": "status=200 body={\"email\":\"admin@juice-sh.op\",\"role\":\"admin\"...}",
            "source_agent": "access_test"
        }
    ],
    "observation_count": 20,
    "cost_usd": 0.47
}
```

---

## 6. ConfirmEvidenceAgent

**File:** `blackbox_service/agents_v2/confirm_evidence.py`  
**Purpose:** Re-test each suspected finding to prove it's real or mark it as false positive.  
**Produces:** `ConfirmedFinding[]` (status: "confirmed" or "false_positive")

### The hypothesis tagging protocol

This agent uses the `hypothesis` field of the JSON response as a **structured tag** to link
observations back to specific finding IDs. This is the mechanism by which the LLM's
actions are connected to the findings they're confirming.

Three tag formats:
- `"evidence:<finding_id>"` — signals that this action is capturing evidence for this finding
- `"confirm:<finding_id>"` — signals that this http_get is a re-test of this finding
- `"sqlmap_confirm:<finding_id>"` — signals that sqlmap output should be linked to this finding

**Example sequence:**

```
Step 1: LLM decides:
  action_type: "http_get"
  params: {"url": "https://juice-shop.local:3000/api/users/2"}
  hypothesis: "confirm:sf-3a4f8b2c1d"

→ http_get returns status 200, body contains user email
→ _after_observation sees hypothesis="confirm:sf-3a4f8b2c1d"
→ status_code==200 → confirm_ok["sf-3a4f8b2c1d"] = True

Step 2: LLM decides:
  action_type: "snapshot"
  params: {}
  hypothesis: "evidence:sf-3a4f8b2c1d"

→ snapshot captured, artifact saved to disk
→ _after_observation sees hypothesis="evidence:sf-3a4f8b2c1d"
→ confirm_ok["sf-3a4f8b2c1d"] == True → creates ConfirmedFinding
```

### What `_after_observation()` does with tags

```python
if note.startswith("confirm:"):
    fid = note.split(":", 1)[1]
    status_code = result.get("status_code", 0)
    if status_code == 200:
        local_state["confirm_ok"][fid] = True
    else:
        local_state["confirm_ok"][fid] = False

if note.startswith("evidence:"):
    fid = note.split(":", 1)[1]
    matched = next((x for x in suspected if x.finding_id == fid), None)
    confirmed = local_state.get("confirm_ok", {}).get(fid, False)

    if confirmed:
        # Create ConfirmedFinding with evidence
        local_state["confirmed"].append(ConfirmedFinding(
            finding_id=matched.finding_id,
            status="confirmed",
            confidence=max(8, matched.confidence),
            evidence=[
                FindingEvidence(kind="http_check", detail=matched.evidence_snippet),
                FindingEvidence(kind="screenshot", artifact_path=result.get("path")),
            ]
        ))
    else:
        # Create ConfirmedFinding marked as false positive
        local_state["false_positives"].append(ConfirmedFinding(
            status="false_positive",
            impact="Could not reproduce under confirmation pass."
        ))
```

### Post-approval sqlmap processing

When the engagement is approved and `sqlmap_probe` is available:

```python
if action_type == "sqlmap_probe":
    ok = bool(obs.get("ok", False))
    stdout = tool_result.get("stdout", "")

    if ok:  # sqlmap found injection point
        local_state["confirmed"].append(ConfirmedFinding(
            vuln_type="sql_injection",
            confidence=10,  # sqlmap confirmation = maximum confidence
            impact="SQL injection confirmed by sqlmap; database access may be possible.",
            evidence=[FindingEvidence(kind="tool_output", detail=stdout[:600])]
        ))
    else:   # sqlmap ran but found nothing
        local_state["false_positives"].append(ConfirmedFinding(
            impact="sqlmap probe did not confirm injection.",
            status="false_positive"
        ))
```

### Auto-tagging for untagged snapshots

If the LLM takes a `snapshot` action without tagging it properly, the code auto-tags it:
```python
if action == "snapshot" and not note.startswith("evidence:"):
    idx = len([o for o in observations if o.get("action_type") == "snapshot"])
    if idx < len(suspected):
        note = f"evidence:{suspected[idx].finding_id}"
```

This prevents snapshots from being wasted on unlinked evidence.

---

## 7. Browser Interaction Engine (BIE)

**File:** `blackbox_service/bie/engine.py`  
**Purpose:** Abstract away the difference between HTTP requests, browser automation, and AI-driven navigation.

### The three tiers

**Tier 1 — Raw HTTP (`httpx`)**

Actions: `http_get`, `http_post`, `http_probe`

```python
# Example: LLM requests http_get on a URL
req = BIERequest(
    run_id="run-abc123",
    goal="Check if /api/users returns data without auth",
    action_type="http_get",
    params={"url": "https://juice-shop.local:3000/api/users"}
)

# BIE sends:
with httpx.Client(timeout=15.0, follow_redirects=True) as client:
    response = client.get(url, headers={
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)...",
        "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })

# Returns:
BIEOutcome(
    ok=True,
    tier_used=1,
    result={"status_code": 200, "url": "...", "headers": {...}, "body_preview": "{\"data\":[...]}"},
    cost_usd=0.0001
)
```

Tier 1 is fast (no browser overhead) and good for probing endpoints directly.
Cost estimate: $0.0001 per call.

**Tier 2 — Playwright browser automation**

Actions: `navigate`, `click`, `fill`, `select_option`, `wait_for_selector`, `get_page_content`,
`eval_js`, `inject_html`, `read_console`, `read_network`, `snapshot`, `open_tab`, `switch_tab`

```python
# Example: LLM wants to navigate to login page
req = BIERequest(
    action_type="navigate",
    params={"url": "https://juice-shop.local:3000/#/login"}
)

# BIE calls execute_action which calls PlaywrightRuntime:
page.goto(url, wait_until="domcontentloaded")
# → TabState(url="https://juice-shop.local:3000/#/login", title="OWASP Juice Shop")
```

Tier 2 runs in a real Chromium browser — JavaScript executes, redirects are followed,
SPAs render properly. This is essential for modern web apps.
Cost estimate: $0.001 per call.

**Tier 4 — AI-driven navigation (`browser-use`)**

Actions: `ai_navigate`

When the LLM encounters a complex flow it can't handle with simple navigate/fill steps
(e.g., a multi-step OAuth flow, a CAPTCHA-protected login, a dynamically loaded form),
it can escalate to Tier 4:

```python
req = BIERequest(
    action_type="ai_navigate",
    params={
        "instruction": "Log in with username admin@juice-sh.op and password admin123",
        "target_url": "https://juice-shop.local:3000",
        "max_steps": 12
    }
)

# BIE spawns a browser-use Agent with Claude as the LLM:
llm = ChatAnthropic(model=self._anthropic_model, api_key=self._anthropic_api_key, max_tokens=4096)
agent = Agent(task=task, llm=llm, browser_profile=BrowserProfile(headless=True))
history = await agent.run(max_steps=max_steps)

# Returns navigation path, URLs visited, and final result
```

Tier 4 is expensive ($0.02 per call) and slow but handles anything a human could do in
a browser. It's used sparingly — only when Tier 2 actions can't complete the goal.

### Middleware delay

Every BIE request adds a randomized delay before execution:
```python
def _apply_middleware_delay(self) -> None:
    delay_s = max(0.05, min(random.gauss(0.20, 0.12), 0.8))
    time.sleep(delay_s)
```
Mean 200ms, σ 120ms, clamped to 50ms–800ms. This mimics human browsing behaviour
and avoids being rate-limited or detected by WAFs that look for machine-speed requests.

---

## 8. SecurityToolGate

**File:** `blackbox_service/toolchannel/security_gate.py`  
**Purpose:** The mandatory policy layer between agents and HexStrike.

Every tool call — regardless of which agent, which tool, or what parameters — flows through
exactly five checks in sequence. If any check fails, the call is rejected and an audit event
is recorded. No agent ever calls HexStrikeClient directly.

### The five checks in order

**Check 1 — Scope**

The target must be within the engagement's origin host. Prevents an agent from accidentally
(or maliciously) scanning unrelated systems.

```python
target = _extract_scope_target(params)
# Checks params["target"] → params["url"] → params["domain"] → params["host"] → params["site"]
# Falls back to empty string if none found

if not _in_scope(tool, target, engagement.target_url):
    # → "out_of_scope" rejection
```

The scope check normalizes `www.example.com` ≡ `example.com` (a previous bug where
`www.blogger.com` was rejected for a `blogger.com` engagement).

For host-level tools (nmap, subfinder): only hostname must match, port irrelevant.
For URL-level tools (nuclei, katana, sqlmap): hostname AND port must match.

**Check 2 — Approval**

Some tools are gated until a human approves:
```python
_GATED_TOOLS = frozenset({"sqlmap", "sqlmap_probe", "metasploit", "exploit"})

if tool in _GATED_TOOLS and not engagement.approval_granted:
    # → "requires_hitl_approval" rejection
```

**Check 3 — Budget**

Tool spend is tracked in a SEPARATE pool from the LLM budget. This check is atomic:
```python
with self._lock:  # threading.Lock
    if engagement.tool_spent_usd + est_cost > self._hard_cap:
        # → "budget_exhausted" rejection
    engagement.tool_spent_usd += est_cost  # reserve atomically
```

Cost estimates per tool: nmap=$0.02, nuclei=$0.05, sqlmap=$0.10, unknown=$0.05.

**Check 4 — Pre-create audit record**

Before executing anything, a `ToolInvocation` record is written to the engagement:
```python
invocation = ToolInvocation(
    tool_name=tool,
    target=target,
    args={k: v for k, v in params.items() if k != "target"},
    started_at=datetime.now(timezone.utc),
)
engagement.tool_invocations.append(invocation)
```

If the process is killed mid-execution, this record shows what was in-flight.

**Check 5 — Cleanup registration**

An expected artifact path is registered before execution:
```python
expected_artifact = str(artifact_dir / f"{tool}_{int(time.time())}.out")
self._pending[pending_key] = expected_artifact
```

On crash, `cleanup()` removes any `_pending` entries. On success, the key is removed.
On failure, the artifact file is deleted and the budget is refunded:
```python
if not invocation.ok:
    Path(expected_artifact).unlink(missing_ok=True)
    engagement.tool_spent_usd = max(0.0, engagement.tool_spent_usd - est_cost)
```

### Audit events

Every decision (pass or reject) emits an event through the event sink:
```python
self._emit("tool.invoked",  {"tool": tool, "ok": True,  "cost_usd": 0.05, ...})
self._emit("tool.rejected", {"tool": tool, "reason": "out_of_scope", ...})
```

These events flow through `EngagementEventBus` to the Ops Console SSE stream,
which is why Tool Activity appears in real time in the right sidebar.

---

## 9. HexStrikeClient and the MCP Protocol

**File:** `blackbox_service/toolchannel/hexstrike_client.py`

### Architecture

HexStrike runs two servers in one Docker container:
- Flask HTTP API on port 8888 (`hexstrike_server.py`) — executes actual security tools
- FastMCP HTTP server on port 8001 (`hexstrike_mcp.py`) — MCP JSON-RPC interface

Our client talks to port 8001 (MCP). The Flask server is only accessible internally
(hexstrike_mcp.py calls it at `http://localhost:8888`).

### Health check

```python
def health(self) -> bool:
    resp = httpx.get(f"{self._base_url}/health", timeout=5.0)
    return 200 <= resp.status_code < 300
```

Called on startup and cached with 30-second TTL. If HexStrike is down, the badge
shows "Tools: OFFLINE" and agents fall back to BIE-only mode (no tool actions sent to gate).

### Tool discovery — `list_tools()`

Primary path: MCP `tools/list`
```python
resp = httpx.post(
    f"{self._mcp_url}/mcp",
    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    headers={"content-type": "application/json", "accept": "application/json"},
    timeout=10.0
)
# Response:
{
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "tools": [
            {"name": "nmap_scan", "description": "Execute an enhanced Nmap scan...",
             "inputSchema": {"properties": {"target": {"type": "string"}, ...}}},
            {"name": "gobuster_scan", "description": "Execute Gobuster to find directories...",
             "inputSchema": {"properties": {"url": {"type": "string"}, ...}}},
            ...  # 151 tools total
        ]
    }
}
```

Fallback path: if MCP server isn't ready yet, falls back to `GET /health` which returns
`tools_status: {"nmap": true, "gobuster": true, ...}` — a dict of tool_name → available.

### Tool invocation — `invoke()`

```python
resp = httpx.post(
    f"{self._mcp_url}/mcp",
    json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "nuclei_scan",
            "arguments": {"target": "https://juice-shop.local:3000", "severity": "medium"}
        }
    },
    headers={"content-type": "application/json", "accept": "application/json"},
    timeout=self._timeout_s  # 300s default
)
```

The `json_response=True` and `stateless_http=True` FastMCP settings mean each POST is
processed independently and returns plain JSON (no SSE streaming). This is required because
our client is a simple synchronous httpx call without SSE parsing capability.

### Response normalization

The MCP response format:
```json
{
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [{"type": "text", "text": "Nmap scan output:\n3000/tcp open http..."}]
    }
}
```

Normalized to our standard shape:
```python
stdout = "\n".join(c["text"] for c in content if c["type"] == "text")
return {"ok": True, "raw": result, "stdout": stdout, "artifacts": [], "error": None}
```

---

## 10. HexStrike Architecture

HexStrike AI v6.0 is a separate open-source project cloned into `hexstrike/`.

### Two-process Docker container

```
hexstrike/ (Docker container)
├── hexstrike_server.py     Flask HTTP API on :8888 — 156 routes, runs actual tools
│                          GET  /health              → tool availability map
│                          POST /api/tools/nmap      → runs: nmap -T4 --open {target}
│                          POST /api/tools/nuclei    → runs: nuclei -u {target} -s medium
│                          POST /api/tools/gobuster  → runs: gobuster dir -u {url}
│                          POST /api/intelligence/smart-scan → AI multi-tool orchestration
│                          ... (156 routes total)
│
└── hexstrike_mcp.py        FastMCP server on :8001 — wraps 151 tools as @mcp.tool() functions
                           POST /mcp                → MCP JSON-RPC (tools/list, tools/call)
                           Each @mcp.tool() function calls Flask at localhost:8888
```

`start.sh` coordinates startup:
1. Start Flask server (`hexstrike_server.py`)
2. Wait for it to respond on `/health` (up to 30 retries, 1s each)
3. Start FastMCP server (`hexstrike_mcp.py --transport streamable-http --mcp-host 0.0.0.0 --mcp-port 8001`)

### Key FastMCP settings

```python
mcp.settings.host = "0.0.0.0"           # listen on all interfaces (needed for Docker)
mcp.settings.port = 8001
mcp.settings.json_response = True        # return plain JSON, not SSE streams
mcp.settings.stateless_http = True       # each request independent, no Mcp-Session-Id needed
mcp.settings.transport_security.enable_dns_rebinding_protection = False  # allow Docker hosts
```

`json_response=True` + `stateless_http=True` is critical. Without these:
- Without `json_response`: server requires `Accept: application/json, text/event-stream` and returns SSE
- Without `stateless_http`: server requires `Mcp-Session-Id` header (HTTP 400 without it)

### Why FastMCP exists alongside Flask

Flask provides the actual tool execution. FastMCP provides the MCP protocol layer that
Claude/GPT/other AI clients can use directly (Claude Desktop, etc.). In our case,
we call the MCP layer ourselves rather than having Claude Desktop do it.

This means: when our client calls `tools/call` with `nmap_scan`, FastMCP's `nmap_scan()`
function runs, which calls `POST http://localhost:8888/api/tools/nmap` on the Flask server,
which runs the actual `nmap` binary and returns the output.

---

## 11. EngagementOrchestrator

**File:** `blackbox_service/orchestrator.py`  
**Purpose:** Manages the entire lifecycle of a security engagement.

### State machine

```
"created"
    ↓  POST /engagements/{id}/start
"running" / current_phase="discovery"
    ↓  DiscoveryAgent.run() completes
"running" / current_phase="access_test"
    ↓  AccessTestAgent.run() completes
    ↓  (if approval_mode=mandatory OR optional+findings+not-approved)
"paused_for_approval"
    ↓  POST /engagements/{id}/approval {"approved": true}
"running" / current_phase="confirm_evidence"
    ↓  ConfirmEvidenceAgent.run() completes
"running" / current_phase="report"
    ↓  ExecutiveReport generated
"completed" / current_phase="done"
```

Rejection path:
```
"paused_for_approval"
    ↓  POST /engagements/{id}/approval {"approved": false}
"completed" / current_phase="done"  (no confirmation phase run)
```

### Thread model

Each engagement runs in a daemon thread:
```python
thread = threading.Thread(
    target=self._run_flow,
    args=(engagement_id, max_steps_per_agent, step_delay_ms, model),
    daemon=True,
    name=f"engagement-{engagement_id}",
)
```

The FastAPI endpoints (create_engagement, start_engagement, approve) are synchronous
and return immediately. The actual work happens in background threads.

Thread safety: all engagement state is guarded by `self._lock = threading.Lock()`.
The SecurityToolGate has its own `self._lock` for budget atomicity.

### Data passing between phases

```python
# Discovery → AccessTest
ctx_access = AgentContext(
    state={
        "discovery_endpoints": rec.attack_surface.endpoints,  # from DiscoveryAgent output
        "available_tools": self._get_available_tools(),         # HexStrike tool list
    }
)

# AccessTest → ConfirmEvidence
ctx_confirm = AgentContext(
    state={
        "suspected_findings": [x.model_dump() for x in rec.suspected_findings],
        "available_tools": self._get_available_tools(),
    }
)
```

### Budget tracking

Two separate budget pools:

**LLM budget** (`rec.budget.spent_usd`):
- Tracks LLM token costs (estimated)
- Incremented by `self._spend(rec, cost)` after each agent phase
- Triggers warnings at 80%, pauses at 95%, terminates at 100%

**Tool budget** (`rec.tool_spent_usd`):
- Tracks HexStrike tool costs
- Managed exclusively by SecurityToolGate under its own lock
- Hard cap set in `.env` as `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` (default $5.00)
- These are separate so tool costs don't eat into the LLM budget

### Reachability cache

```python
def _live_hexstrike_reachable(self) -> bool:
    now = time.monotonic()
    with self._reachability_lock:
        if now - self._last_reachability_ts >= 30.0:  # 30s TTL
            self._hexstrike_reachable = self._hexstrike_client.health()
            if self._hexstrike_reachable:
                self._available_tools = self._hexstrike_client.list_tools()
            self._last_reachability_ts = now
        return self._hexstrike_reachable
```

Called every 30 seconds at most. Prevents hammering HexStrike's `/health` endpoint.
The badge in Ops Console auto-refreshes via JavaScript `setInterval(refreshCapabilities, 30000)`.

### Executive report generation

After ConfirmEvidence completes:
```python
def _build_report(rec: EngagementRecord) -> ExecutiveReport:
    overview = Counter(f.severity for f in rec.confirmed_findings)
    return ExecutiveReport(
        title=f"Security Engagement Report — {rec.target_url}",
        target=rec.target_url,
        engagement_id=rec.engagement_id,
        summary=(
            f"Automated blackbox assessment identified {len(rec.confirmed_findings)} "
            f"confirmed vulnerabilities ({overview.get('critical',0)} critical, ...)."
        ),
        findings_overview=dict(overview),
        key_risks=[f.title for f in rec.confirmed_findings if f.severity in ("critical","high")][:5],
        recommendations=[
            "Immediately patch all critical and high severity findings.",
            "Implement input validation and parameterized queries.",
            ...
        ]
    )
```

---

## 12. BlackboxService

**File:** `blackbox_service/service.py`

This service manages the lower-level "runs" — the browser sessions and single-agent loops
used by the technical dashboard (not the engagement pipeline). It's also used by the
engagement pipeline as the underlying browser runtime.

### Key responsibilities

1. **Run management** — creates, stores, and tracks browser runs
2. **Agent loop** — runs the single-agent loop (for technical dashboard)
3. **Action execution** — translates action names to runtime calls
4. **Event streaming** — publishes events to `RunEventBus`

### The planner hierarchy

For the single-agent mode (technical dashboard):
```
build_planner() decides:
├── Both API keys → FailoverPlanner(AnthropicPlanner, GeminiPlanner)
│                   Tries Anthropic first; switches to Gemini on any exception
│                   Sticky: once switched, stays on Gemini for the run
├── Anthropic only → AnthropicPlanner
├── Gemini only    → GeminiPlanner
└── Neither        → RuleBasedPlanner (4 fixed steps: console → network → eval_js → snapshot)
```

`AnthropicPlanner` and `GeminiPlanner` call their respective LLM APIs with a comprehensive
security testing system prompt (different from agents_v2 — this is the single-agent version).

### Action execution

```python
def execute_action(self, run_id, action_type, params) -> dict:
    match action_type:
        case "open_tab":
            tab = self._runtime.open_tab(run_id, params["url"], ...)
            self._store.upsert_tab(tab)
            return {"result": tab.model_dump()}
        case "navigate":
            tab = self._runtime.navigate_tab(run_id, tab_id, params["url"])
            return {"result": tab.model_dump()}
        case "snapshot":
            path = self._runtime.capture_screenshot(run_id, tab_id, artifact_name)
            self._bus.publish(EventEnvelope(type="artifact.screenshot", payload={"path": path}))
            return {"result": {"path": path}}
        ...
```

---

## 13. Runtime Layer

**File:** `blackbox_service/runtime.py`

Three runtime implementations with the same interface:

### InMemoryRuntime

Used for testing and offline capability. Stores tab state in Python dicts.
`eval_js()` uses a safe AST-based evaluator for simple arithmetic only:
```python
"1 + 1"  → 2      (works)
"alert(1)"  → ValueError  (blocked)
```
`capture_screenshot()` writes a text placeholder file instead of a real screenshot.

### PlaywrightRuntime

Real Chromium browser. Each run gets its own `BrowserContext` (isolated cookies, storage).
Each tab is a Playwright `Page` object.

Console and network events are captured via Playwright hooks:
```python
page.on("console", lambda msg: state.console_logs[tab_id].append({"type": msg.type, "text": msg.text}))
page.on("request",  lambda req: state.network_events[tab_id].append({"method": req.method, "url": req.url}))
page.on("response", lambda res: state.network_events[tab_id].append({"status": res.status, "url": res.url}))
```

`get_page_content()` runs a JavaScript snippet to extract:
```javascript
{
    url: location.href,
    title: document.title,
    text: document.body.innerText.slice(0, 4000),
    inputs: Array.from(document.querySelectorAll('input,textarea,select,button')).map(el => ({
        tag: el.tagName.toLowerCase(), type: el.type, name: el.name, id: el.id,
        placeholder: el.placeholder, text: el.textContent?.trim().slice(0, 80)
    })),
    links: Array.from(document.querySelectorAll('a[href]')).map(a => a.href).slice(0, 30)
}
```

### ThreadedPlaywrightRuntime

Playwright's sync API is not thread-safe. When the engagement orchestrator (running in a
background thread) needs browser access, it can't call PlaywrightRuntime directly.

`ThreadedPlaywrightRuntime` solves this with a single owner thread and a task queue:
```python
def _worker(self, headless, artifacts_dir):
    runtime = PlaywrightRuntime(headless=headless, artifacts_dir=artifacts_dir)
    while True:
        method_name, args, kwargs, out_q = self._tasks.get()  # blocks until task arrives
        if method_name is None: break
        out_q.put((True, getattr(runtime, method_name)(*args, **kwargs)))

def _call(self, method_name, *args, **kwargs):
    out_q = queue.Queue(maxsize=1)
    self._tasks.put((method_name, args, kwargs, out_q))
    ok, value = out_q.get()  # blocks until result returns
    return value if ok else raise value

def __getattr__(self, name):
    return lambda *args, **kwargs: self._call(name, *args, **kwargs)
```

Any thread calling `runtime.navigate_tab(...)` goes through `_call()` → task queue →
owner thread executes → result returned. All Playwright calls happen on the owner thread.

---

## 14. SQLite Event Store

**File:** `blackbox_service/store.py`

### Schema

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,           -- "running", "stopped", "completed"
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    targets_json TEXT NOT NULL,     -- JSON array of target URLs
    options_json TEXT NOT NULL,     -- JSON object of run options
    active_tab_id TEXT,             -- currently focused tab
    error TEXT                      -- error message if status="failed"
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,  -- UUID
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,               -- ISO-8601 UTC
    type TEXT NOT NULL,             -- "agent.reasoning", "action.click", etc.
    tab_id TEXT,
    step_id TEXT,
    payload_json TEXT NOT NULL,     -- arbitrary JSON payload
    token_cost REAL                 -- LLM token cost for this event (if applicable)
);

CREATE INDEX idx_events_run_id_id ON events(run_id, id);    -- pagination queries
CREATE INDEX idx_events_run_id_type ON events(run_id, type); -- type filtering

CREATE TABLE tabs (
    run_id TEXT NOT NULL,
    tab_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    parent_tab_id TEXT,
    correlation_id TEXT,
    is_active INTEGER NOT NULL,     -- 0 or 1
    opened_at TEXT NOT NULL,
    PRIMARY KEY(run_id, tab_id)
);
```

### Why events are append-only

Every action, observation, and agent reasoning step is stored as an immutable event.
This provides:
- **Audit trail** — complete record of everything the agent did
- **Replay capability** — the SSE stream can replay all events to a late-joining browser
- **Debugging** — if something went wrong, the full sequence is preserved

The event type names follow a hierarchical convention:
```
agent.thought      → LLM decided what to think
agent.hypothesis   → LLM formed a hypothesis
agent.reasoning    → LLM made a decision (thought + hypothesis + action)
agent.step.completed → action executed, result available
action.navigate    → browser navigated to URL
action.click       → browser clicked element
action.fill        → browser filled input
observation.console → console log captured
observation.network → network request/response
artifact.screenshot → screenshot saved to disk
agent.started      → agent loop began
agent.finished     → agent loop completed
agent.failed       → agent loop crashed
run.started        → run created
run.stopped        → run terminated
```

---

## 15. Event Bus and SSE Streaming

Two separate event buses for two different contexts:

### RunEventBus (for single-agent / technical dashboard)

**File:** `blackbox_service/stream.py`

Simple in-memory list with an async generator interface:
```python
class RunEventBus:
    def __init__(self):
        self._events: dict[str, list[EventEnvelope]] = defaultdict(list)
        self._lock = threading.Lock()

    def publish(self, event: EventEnvelope) -> None:
        with self._lock:
            self._events[event.run_id].append(event)

    async def subscribe(self, run_id: str):  # async generator
        cursor = 0
        while True:
            with self._lock:
                batch = self._events.get(run_id, [])[cursor:]
                cursor += len(batch)
            for event in batch:
                yield event
            await asyncio.sleep(0.05)  # poll every 50ms
```

Used by `GET /runs/{run_id}/stream` — the technical dashboard SSE endpoint.

### EngagementEventBus (for engagement / Ops Console)

**File:** `blackbox_service/engagement_bus.py`

Thread-safe fan-out bus using `queue.Queue` per consumer:
```python
class EngagementEventBus:
    def publish(self, engagement_id: str, msg: dict) -> None:
        with self._lock:
            queues = list(self._subscribers.get(engagement_id, []))
        for q in queues:
            try:
                q.put_nowait(msg)      # non-blocking
            except queue.Full:
                pass                   # drop if consumer is slow

    def subscribe(self, engagement_id: str) -> queue.Queue:
        q = queue.Queue(maxsize=512)
        with self._lock:
            self._subscribers[engagement_id].append(q)
        return q
```

Used by `GET /engagements/{engagement_id}/stream` — the Ops Console SSE endpoint.

### SSE stream for engagements

The stream endpoint:
1. **Replays history** — sends all events already in `rec.events` list (so late-joining browser sees everything)
2. **Subscribes for live events** — creates a consumer queue
3. **Polls queue** — drains `q.get_nowait()` in a loop with `asyncio.sleep(0.05)` between polls
4. **Terminates** when engagement reaches a terminal status ("completed", "failed", "budget_exhausted")
5. **Unsubscribes** in a `finally` block to prevent memory leaks

```python
async def generate():
    for evt in list(rec.events):          # replay history
        yield f"data: {json.dumps(enriched)}\n\n"
    if rec.status in _TERMINAL_STATUSES:
        return
    q = bus.subscribe(engagement_id)
    try:
        while True:
            try:
                msg = q.get_nowait()
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("status") in _TERMINAL_STATUSES:
                    break
            except queue.Empty:
                await asyncio.sleep(0.05)
    finally:
        bus.unsubscribe(engagement_id, q)
```

---

## 16. API Layer

**File:** `blackbox_service/api.py`

The `create_app()` factory creates a FastAPI application and registers all routes.
It also serves the dashboard HTML inline (the HTML is a 1300-line f-string template).

### Endpoint groups

**Health and Config**
- `GET /health` — runtime info, capabilities (LLM key configured, HexStrike reachable), used by Ops Console badges
- `GET /config/models` — available LLM models with availability status based on configured API keys

**Run management** (technical dashboard / single-agent)
- `POST /runs` — create a new browser session
- `GET /runs/{run_id}` — run status
- `GET /runs/{run_id}/tabs` — all browser tabs
- `POST /runs/{run_id}/actions` — execute any browser action
- `GET /runs/{run_id}/memory` — all events for a run
- `GET /runs/{run_id}/artifacts` — list screenshots/outputs
- `POST /runs/{run_id}/stop` — terminate run
- `POST /runs/{run_id}/agent/start` — start single-agent loop
- `GET /runs/{run_id}/agent/state` — agent loop status
- `GET /runs/{run_id}/stream` — SSE stream of run events

**Engagement management** (Ops Console / enterprise mode)
- `POST /engagements` — create engagement (target, budget, approval mode)
- `POST /engagements/{id}/start` — begin the 3-phase pipeline
- `GET /engagements/{id}` — engagement status + attack surface + findings
- `GET /engagements/{id}/events` — all events
- `POST /engagements/{id}/approval` — approve or reject (HITL gate)
- `GET /engagements/{id}/findings` — suspected + confirmed findings
- `GET /engagements/{id}/report` — executive report
- `GET /engagements/{id}/tool-invocations` — all HexStrike tool calls with timing/cost
- `GET /engagements/{id}/stream` — SSE stream of live engagement events

**UI routes**
- `GET /` → redirect to `/dashboard`
- `GET /dashboard` — technical dashboard (single-agent mode)
- `GET /engagement-dashboard` — executive dashboard (engagement mode)
- `GET /ops-console` — cinematic SSE Operations Console
- `GET /static/*` — CSS/JS assets for Ops Console
- `GET /artifacts/{run_id}/{filename}` — serve screenshot images

---

## 17. Ops Console Frontend

**File:** `blackbox_service/static/ops_console.html`, `ops_console.js`, `ops_console.css`

The Ops Console is the primary UI for Phase A engagements. It connects to the SSE stream
and updates the interface in real time.

### Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ Header: target input | approval mode | budget | steps | model  │
│         Create | Start | Approve | Reject | View Report        │
│         Tools: ON/OFF  LLM: ON/OFF                             │
├────────────────────────────────────┬────────────────────────────┤
│ Glow Column (left 60%)             │ Panels (right 40%)         │
│                                    │                            │
│  NOW BAR: "Phase: discovery"       │  STATUS panel              │
│  "Running nmap scan..."            │    Status: running         │
│                                    │    Phase: discovery        │
│  STEP [discovery] #3 →             │    Budget: $0.02/$10       │
│  nmap_scan: 3000/tcp open http...  │    Tool calls: 3           │
│                                    │    Tool spend: $0.05/$5    │
│  STEP [discovery] #4 →             │                            │
│  subfinder: api.target.com         │  TOOL ACTIVITY panel       │
│                                    │    nmap_scan → ok 1502ms   │
│  APPROVAL: Waiting for approval    │    nuclei_scan → ok 37ms   │
│  6 suspected findings found.       │                            │
│  [Approve] [Reject]                │  FINDINGS panel            │
│                                    │    Suspected: 6            │
│                                    │    Confirmed: 2            │
│                                    │    [IDOR finding card]     │
│                                    │    [XSS finding card]      │
└────────────────────────────────────┴────────────────────────────┘
```

### SSE connection lifecycle

```javascript
function openStream(engagementId) {
    const source = new EventSource(`/engagements/${engagementId}/stream`);
    source.addEventListener("message", (evt) => {
        const data = JSON.parse(evt.data);
        // data.type: "agent.step", "phase.start", "tool.invoked", etc.
        // data.phase: current phase
        // data.status: engagement status
        // data.budget: {spent, limit, tool_spent, tool_cap}
        updateUI(data);
    });
}
```

### Capabilities badge refresh

Both the Tools and LLM badges are refreshed every 30 seconds:
```javascript
async function refreshCapabilities() {
    const data = await fetch("/health").then(r => r.json());
    const on = data.capabilities.tool_channel_enabled && data.capabilities.hexstrike_reachable;
    badge.textContent = on ? "Tools: ON" : `Tools: OFFLINE`;
}
setInterval(refreshCapabilities, 30_000);  // every 30 seconds
```

This means if you start HexStrike after the browser is already open, the badge
updates within 30 seconds without a page reload.

---

## 18. Settings and Configuration

**File:** `blackbox_service/settings.py`

**Critical:** API keys are loaded from the `.env` file ONLY, never from shell environment.
This is intentional — prevents accidental key leakage via `export ANTHROPIC_API_KEY=...`.

```python
def load_settings(env_file=".env"):
    file_values = _parse_env_file(env_file)  # reads .env file directly

    def pick(name, default):
        # For API keys: file-only (security requirement)
        if name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            return file_values.get(name, default)
        # For everything else: file first, then env var
        return file_values.get(name) or os.getenv(name) or default
```

### Every configuration variable

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `ANTHROPIC_API_KEY` | (required for LLM) | Claude API access |
| `GEMINI_API_KEY` | (optional) | Gemini fallback |
| `BLACKBOX_AGENT_MODEL` | `claude-sonnet-4-6` | LLM model for all agents |
| `BLACKBOX_GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model (fallback) |
| `BLACKBOX_HOST` | `127.0.0.1` | FastAPI bind host |
| `BLACKBOX_PORT` | `8080` | FastAPI bind port |
| `BLACKBOX_USE_PLAYWRIGHT` | `true` | Use real browser (false = InMemory) |
| `BLACKBOX_BROWSER_HEADLESS` | `false` | Run browser headlessly (true in Docker) |
| `BLACKBOX_STRICT_PLAYWRIGHT_RUNTIME` | `false` | Fail hard if Playwright unavailable |
| `BLACKBOX_TARGET_URL` | `http://localhost:3000` | Default target URL in dashboard |
| `BLACKBOX_AGENT_MAX_STEPS` | `20` | Steps per agent (all 3 phases share this) |
| `BLACKBOX_AGENT_STEP_DELAY_MS` | `1000` | Milliseconds between agent steps |
| `BLACKBOX_HEXSTRIKE_ENABLED` | `true` | Enable HexStrike tool channel |
| `BLACKBOX_HEXSTRIKE_URL` | `http://localhost:8888` | HexStrike Flask server URL |
| `BLACKBOX_HEXSTRIKE_TIMEOUT_S` | `300` | Max seconds for a single tool call |
| `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` | `5.0` | Max total tool spend per engagement |
| `BLACKBOX_AUTO_OPEN_BROWSER` | `true` | Auto-open browser on demo launch |
| `BLACKBOX_DB_PATH` | `blackbox_events.db` | SQLite database path |

---

## 19. Budget System

Two completely independent budget pools, tracked separately:

### LLM Budget (`EngagementRecord.budget`)

```python
class BudgetState(BaseModel):
    limit_usd: float = 50.0         # set at engagement creation
    spent_usd: float = 0.0          # incremented after each agent phase
    warn_threshold: float = 0.80    # logs warning at 80%
    pause_threshold: float = 0.95   # pauses engagement at 95%
```

The orchestrator calls `self._spend(rec, cost)` after each agent phase completes.
The cost passed in is the `cost_usd` from the agent's summary (estimated based on
number of LLM calls × model cost per call).

Threshold behaviour:
- 80%: emits `budget.warning` event (shown in Ops Console)
- 95%: emits `budget.critical` event, pauses engagement
- 100%: terminates engagement with status `budget_exhausted`

### Tool Budget (`EngagementRecord.tool_spent_usd`)

Separate float field on the engagement record. Managed exclusively by SecurityToolGate
under a `threading.Lock()`. Never mixed with LLM budget.

Cost map (estimates for budget tracking, not actual charges):
```python
_TOOL_COST_MAP = {
    "nmap_scan": 0.02,      # fast scan
    "nuclei_scan": 0.05,    # moderate (runs multiple templates)
    "katana_crawl": 0.03,   # crawling
    "subfinder_enum": 0.01, # DNS-only, cheap
    "sqlmap_probe": 0.10,   # slow, multiple requests
    # Unknown tools: 0.05 default
}
```

Hard cap (`BLACKBOX_TOOL_BUDGET_HARD_CAP_USD=5.0`) prevents runaway tool costs.
When a tool's estimated cost would exceed the cap, it's rejected before execution.

---

## 20. HITL Approval Gate

HITL = Human In The Loop. The gate prevents destructive confirmation tools (sqlmap, etc.)
from running without explicit human consent.

### When the gate triggers

```python
# In _run_flow(), after AccessTestAgent completes:
has_findings = len(rec.suspected_findings) > 0
needs_pause = (
    rec.approval_mode == "mandatory"
    or (rec.approval_mode == "optional" and has_findings and not rec.approval_granted)
)
```

- `mandatory`: always pauses, even with zero findings
- `optional` (default): pauses only if there are findings AND approval hasn't been granted yet
- `none`: never pauses, runs confirmation automatically

The gate is **idempotent** — `approval_granted=True` check prevents a second pause if
the engagement somehow re-enters the approval phase.

### What the Ops Console shows

```
APPROVAL ■ 07:01:08
WAITING FOR APPROVAL — 6 suspected finding(s) found.
Review the FINDINGS panel, then Approve to run confirmation probes or Reject to close.

[Approve — run confirmation]  [Reject — close engagement]
```

### What approval changes

```python
def approve(self, engagement_id, body: ApprovalRequest) -> EngagementRecord:
    rec.approval_granted = bool(body.approved)
    rec.approval_required = False
    rec.current_phase = "confirm_evidence" if body.approved else "done"
    rec.status = "running" if body.approved else "completed"
    self._event(rec, "engagement.approval.updated", {"approved": body.approved, "note": body.note})
    if body.approved:
        self.start_engagement(engagement_id, max_steps_per_agent=8, step_delay_ms=100)
```

When approved:
1. `rec.approval_granted = True` — SecurityToolGate will now pass sqlmap_probe
2. `rec.current_phase = "confirm_evidence"` — orchestrator knows where to resume
3. New background thread started → runs ConfirmEvidenceAgent with sqlmap unlocked

When rejected:
1. `rec.current_phase = "done"` — no confirmation phase
2. Report is generated from suspected findings only (unconfirmed)

---

## 21. Dynamic Tool Discovery

One of the key design wins: the LLM always sees ALL available HexStrike tools, never just
a hardcoded subset.

### The flow

```
1. EngagementOrchestrator starts
   ↓
2. _live_hexstrike_reachable() called
   ↓ (every 30s, TTL-cached)
3. HexStrikeClient.list_tools() called
   POST http://hexstrike:8001/mcp
   {"method": "tools/list"}
   ↓
4. FastMCP returns 151 tool schemas with names, descriptions, inputSchema
   ↓
5. Stored in orchestrator._available_tools = [...]
   ↓
6. On each agent run, injected into AgentContext.state["available_tools"]
   ↓
7. Agent's plan_next() builds allowed_actions from this list
   - DiscoveryAgent: tool_names + _BASE_ALLOWED_ACTIONS
   - AccessTestAgent: tool_names + base_actions
   - ConfirmEvidenceAgent: base_actions + tool_names
   ↓
8. LLM context includes full tool list in "allowed_actions" key
   ↓
9. LLM chooses any tool by name
   ↓
10. AgentBase._effective_tool_names(ctx) = hardcoded | dynamic
    → any tool name from step 9 is in this set
    ↓
11. Routes to SecurityToolGate.invoke()
    ↓
12. HexStrikeClient.invoke() sends tools/call MCP request
```

### The fallback path (HexStrike offline)

If HexStrike is offline, `list_tools()` calls `_list_tools_health_fallback()`:
```python
# GET http://hexstrike:8888/health
# Response: {"tools_status": {"nmap": true, "gobuster": false, "nuclei": true, ...}}
return [{"name": name, "description": f"HexStrike: {name}"} for name, available in tools_status.items() if available]
```

This gives bare tool names without parameter schemas. The LLM uses its training knowledge
to figure out the right parameters.

---

## 22. Phase A vs Phase B

| Aspect | Phase A (`blackbox_service/`) | Phase B (`agents/`, `run_agent.py`) |
|--------|-------------------------------|-------------------------------------|
| Architecture | Service-based (FastAPI + SQLite + HexStrike) | Standalone script |
| Agents | 3 specialized agents in sequence | 1 general-purpose agent |
| Tools | 151 HexStrike tools via MCP | None (browser-only) |
| State | Persisted in SQLite | In-memory only |
| UI | Ops Console (SSE, real-time) | Injected sidebar in browser |
| HITL | Approval gate before confirmation | None |
| Budget | LLM + tool budget tracking | None |
| Findings | `SuspectedFinding` + `ConfirmedFinding` | Text output only |
| Report | Structured `ExecutiveReport` | None |
| Run | `uv run blackbox-agent` + Docker | `python run_agent.py <url>` |
| Best for | Production engagements, demos, teams | Quick single-target exploration |

They share **zero code** (verified by grep — no cross-imports).

---

## 23. Testing Architecture

All tests live in `tests/`. The test suite has 110 tests.

### Test categories

**Tool channel**
- `test_security_gate.py` — scope check math, www normalization, approval gating, budget exhaustion
- `test_hexstrike_client.py` — MCP request/response normalization, timeout handling, fallback

**Agent internals**
- `test_discovery_tools.py` — nmap/katana/subfinder/nuclei output parsing in _after_observation
- `test_access_test_tools.py` — nuclei finding → SuspectedFinding conversion, severity capping
- `test_confirm_tools.py` — sqlmap output → ConfirmedFinding, false positive handling

**API contracts**
- `test_api_contracts.py` — FastAPI endpoints, request/response shapes
- `test_engagement_api.py` — full engagement lifecycle via HTTP
- `test_approval_resume.py` — approval gate → ConfirmEvidence phase transition

**Service layer**
- `test_event_store.py` — SQLite schema, append/query operations
- `test_streaming.py` — RunEventBus publish/subscribe
- `test_runtime_tabs.py` — InMemoryRuntime tab management
- `test_agent_step_sink.py` — SSE event emission from agent loop

**Integration**
- `test_toolchannel_integration.py` — full SecurityToolGate → HexStrikeClient path (mocked HTTP)
- `test_live_tool_path.py` — tool routing through AgentBase

**Reasoning**
- `test_agent_reasoning.py` — ScriptedPlanner with BlackboxService

### How tests mock HexStrike

Tests use `pytest-httpx` to intercept HTTP calls:
```python
def test_invoke_normalizes_response(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://hexstrike-test:8001/mcp",   # the MCP endpoint
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "Nmap scan report..."}]}
        },
    )
    client = HexStrikeClient(base_url="http://hexstrike-test:8888")
    result = client.invoke("nmap_scan", {"target": "example.com"})
    assert result["ok"] is True
    assert result["stdout"] == "Nmap scan report..."
```

### Running tests

```bash
uv run pytest tests/ -q --ignore=tests/test_settings.py
# 110 passed in ~4 seconds
```

`test_settings.py` is ignored because it checks a default model name that's overridden
by the local `.env` file during development.

---

## 24. End-to-End Flow — Complete Example

**Scenario:** Testing OWASP Juice Shop at `http://juice-shop.local:3000`

### Step 0: Startup

```bash
make tools
# → clones hexstrike/ if not present
# → docker compose --profile tools up --build
# → juice-shop starts on :3000
# → hexstrike starts on :8888 (Flask) + :8001 (FastMCP)
# → blackbox-agent starts on :8080

uv run blackbox-agent
# → loads .env
# → builds AnthropicPlanner (claude-sonnet-4-6)
# → creates HexStrikeClient (connects to :8888 health check)
# → list_tools() → fetches 151 tools from :8001/mcp
# → FastAPI starts listening on :8080
```

### Step 1: Create engagement

```
User: opens http://localhost:8080/ops-console
User: types "http://juice-shop.local:3000" in target field
User: sets budget=$10, max_steps=20
User: clicks "Create"

→ POST /engagements
  body: {"target_url": "http://juice-shop.local:3000", "budget_usd": 10, "approval_mode": "optional"}

→ _normalize_engagement_url("http://juice-shop.local:3000") → "http://juice-shop.local:3000" (already has scheme)

→ EngagementRecord created:
  engagement_id: "eng-3f8a2b1c4d5e"
  target_url: "http://juice-shop.local:3000"
  status: "created"
  budget: {limit_usd: 10.0, spent_usd: 0.0}
```

### Step 2: Start engagement

```
User: clicks "Start"

→ POST /engagements/eng-3f8a2b1c4d5e/start
  body: {"max_steps_per_agent": 20, "step_delay_ms": 200, "model": "claude-sonnet-4-6"}

→ background thread spawned: _run_flow(engagement_id, 20, 200, "claude-sonnet-4-6")

→ SecurityToolGate created with:
   - HexStrikeClient targeting :8001
   - reachable=True (from live health check)
   - budget_hard_cap=$5.0

→ BlackboxService.start_run(targets=["http://juice-shop.local:3000"])
   → ThreadedPlaywrightRuntime.open_tab("http://juice-shop.local:3000")
   → Chromium opens, navigates to Juice Shop
   → run_id: "run-ca1a270f9302"
```

### Step 3: Discovery phase

```
rec.current_phase = "discovery"
→ SSE event: {"type": "phase.start", "payload": {"phase": "discovery"}}
→ Ops Console: "PHASE ► Starting phase: discovery"

DiscoveryAgent.run(ctx) starts:
  max_steps=20, target_url="http://juice-shop.local:3000"
  available_tools=[151 tools from HexStrike]
  effective_tool_names={151 HexStrike tools} | {nmap_scan, subfinder, katana, nuclei}

Step 1: plan_next() → LLM call
  context: {"step": 0, "max_steps": 20, "tools_enabled": true, ...}
  LLM responds: {"thought": "Start with port scan", "action_type": "nmap_scan",
                  "params": {"target": "juice-shop.local"}}
  → SecurityToolGate checks:
    ✓ scope: "juice-shop.local" matches "http://juice-shop.local:3000" (hostname only for nmap)
    ✓ approval: nmap not gated
    ✓ budget: $0.00 + $0.02 = $0.02 ≤ $5.00
    → HexStrikeClient.invoke("nmap_scan", {"target": "juice-shop.local"})
    → POST http://hexstrike:8001/mcp: {"method": "tools/call", "params": {"name": "nmap_scan", "arguments": {...}}}
    → FastMCP calls hexstrike_server.py /api/tools/nmap
    → nmap runs: nmap -T4 --open juice-shop.local
    → returns: "3000/tcp open http Node.js"
  → _after_observation: tech_stack.add("Node.js")
  → SSE event: agent.step, tool.invoked
  → Ops Console: "[discovery] #1 → nmap_scan: 3000/tcp open http Node.js"

Step 2: LLM → subfinder_enum
  params: {"domain": "juice-shop.local"}
  scope: hostname match ✓
  → result: empty (local domain, no subdomains)

Step 3: LLM → katana_crawl
  params: {"url": "http://juice-shop.local:3000", "depth": 3}
  scope: "juice-shop.local:3000" matches ✓
  → result: 47 URLs including /api/users, /api/products, /#/login, /#/basket

Step 4-12: LLM probes /robots.txt, /api, /.env, /swagger, /graphql, etc. via http_get
  → endpoints list grows to 23 items
  → nuclei_scan runs on http://juice-shop.local:3000
  → nuclei finds: "missing-csp" (medium), "exposed-jwt-secret" (high→capped to medium)

DiscoveryAgent.summarize() returns:
  hosts: ["juice-shop.local"]
  endpoints: [23 items]
  tech_stack: ["Node.js", "Express"]
  nuclei_findings: [{"template_id": "missing-csp", ...}]
```

### Step 4: Access test phase

```
rec.current_phase = "access_test"
ctx.state["discovery_endpoints"] = 23 endpoints
login_candidates: [{"url": "http://juice-shop.local:3000/#/login", ...}]
api_candidates: [{"url": "http://juice-shop.local:3000/api/users", ...}, ...]

Step 1: LLM decides to test default credentials on /#/login
  action_type: "navigate", params: {"url": "http://juice-shop.local:3000/#/login"}
  → Playwright navigates, page renders Angular SPA

Step 2: LLM uses get_page_content to read the login form
  → finds email and password inputs

Step 3: LLM tries ai_navigate for the login test
  → browser-use Agent logs in with admin@juice-sh.op / admin123
  → succeeds, gets JWT token

Step 4: LLM tests /api/users without auth
  action_type: "http_get", params: {"url": "http://juice-shop.local:3000/api/users"}
  → 200 response with user list including emails
  → _after_observation auto-detects: "/api" in url AND "email" in body → SuspectedFinding added
    finding: missing_auth_api, severity=medium, confidence=5

Step 5-8: LLM probes numeric IDs
  GET /api/orders/1 → 200, GET /api/orders/2 → different user data
  → SuspectedFinding added: idor, severity=high, confidence=6

Step 9: LLM runs gobuster_scan
  params: {"url": "http://juice-shop.local:3000", "mode": "dir", "wordlist": "..."}
  → finds /ftp (200), /admin (403 → working access control)

Step 10-15: XSS testing, path traversal, JWT analysis...
  Multiple tests, LLM marks non-vulnerable cases as "not vulnerable"

AccessTestAgent.summarize():
  suspected_findings: [
    {finding_id: "sf-a1b2c3", vuln_type: "missing_auth_api", severity: "medium", ...},
    {finding_id: "sf-d4e5f6", vuln_type: "idor", severity: "high", ...},
    {finding_id: "sf-g7h8i9", vuln_type: "xss", severity: "high", ...}
  ]
```

### Step 5: Approval gate

```
needs_pause = (approval_mode=="optional" AND has_findings AND NOT approval_granted)
→ True (optional + 3 findings + not approved)

rec.status = "paused_for_approval"
→ SSE event: engagement.approval.needed
→ Ops Console: "WAITING FOR APPROVAL — 3 suspected finding(s)"
→ Approve/Reject buttons become active in UI
```

### Step 6: Human approves

```
User: reviews findings, clicks "Approve — run confirmation"
→ POST /engagements/eng-3f8a2b1c4d5e/approval {"approved": true, "note": "approved"}

rec.approval_granted = True
rec.current_phase = "confirm_evidence"
rec.status = "running"
→ new background thread: _run_flow() resumes at confirm_evidence phase
→ SecurityToolGate: sqlmap_probe now allowed
```

### Step 7: Confirmation phase

```
ConfirmEvidenceAgent.run():
  suspected=[3 findings]
  available_tools includes sqlmap_probe (post-approval)

Step 1: LLM retests missing_auth_api
  action_type: "http_get", params: {"url": "/api/users"}
  hypothesis: "confirm:sf-a1b2c3"
  → 200 with user list → confirm_ok["sf-a1b2c3"] = True

Step 2: LLM takes snapshot
  hypothesis: "evidence:sf-a1b2c3"
  → screenshot saved: artifacts/eng-3f8a2b1c4d5e/snapshot_...png
  → ConfirmedFinding created: status="confirmed", confidence=8

Step 3: LLM uses sqlmap_probe on IDOR endpoint
  action_type: "sqlmap_probe"
  params: {"target": "http://juice-shop.local:3000/api/orders/1"}
  hypothesis: "sqlmap_confirm:sf-d4e5f6"
  → SecurityToolGate: approval_granted=True ✓
  → sqlmap runs, finds injectable parameter
  → ConfirmedFinding created: confidence=10, "SQL injection confirmed by sqlmap"

Step 4: LLM retests XSS finding
  → endpoint no longer returns the XSS reflection
  → confirm_ok["sf-g7h8i9"] = False
  → FalsePositive created: "Could not reproduce under confirmation pass"
```

### Step 8: Report

```
_build_report(rec):
  confirmed_findings: 2
  false_positives: 1
  ExecutiveReport:
    title: "Security Engagement Report — http://juice-shop.local:3000"
    summary: "Automated blackbox assessment identified 2 confirmed vulnerabilities
              (0 critical, 1 high, 1 medium, 0 low)."
    findings_overview: {"high": 1, "medium": 1}
    key_risks: ["IDOR via numeric identifier"]
    recommendations: ["Implement server-side authorization...", "Use parameterized queries..."]

rec.status = "completed"
→ SSE event: engagement.completed
→ Ops Console: "DONE ✓ Complete — 2 confirmed finding(s)"
→ "View Report" button appears green
```

---

## Glossary

| Term | Definition |
|------|-----------|
| BIE | Browser Interaction Engine — abstracts HTTP, Playwright, and AI navigation |
| HITL | Human In The Loop — the approval gate before destructive confirmation |
| MCP | Model Context Protocol — JSON-RPC standard for AI tool integration |
| SSE | Server-Sent Events — one-way HTTP streaming for real-time UI updates |
| SuspectedFinding | A potential vulnerability found by AccessTestAgent, not yet confirmed |
| ConfirmedFinding | A vulnerability confirmed by ConfirmEvidenceAgent with evidence |
| ToolChannel | The directory containing SecurityToolGate + HexStrikeClient |
| Engagement | A complete security assessment lifecycle (discovery → access → confirm → report) |
| Run | A browser session managed by BlackboxService |
| Phase A | The full enterprise engagement service (`blackbox_service/`) |
| Phase B | The standalone browser-use demo agent (`agents/`, `run_agent.py`) |
| step_sink | Callback that receives agent.step events for real-time SSE visibility |
| tool_gate | A SecurityToolGate instance bound to a specific engagement record |
| available_tools | 151 HexStrike tool schemas dynamically fetched at engagement start |
| effective_tool_names | Union of hardcoded + dynamic tool names in AgentBase |
