# Blackbox Agent — Advancements Beyond Original Implementation

> **Reference baseline:** `explanation.md` describes the original system architecture.
> This document covers every advancement made on top of that baseline — what changed, how it works internally, and the complete technical picture.

---

## 1. Critical Bug Fixes

### 1a. Anthropic API 400 Error Fixed
**What was broken:** `AnthropicPlanner` in `blackbox_service/agent.py` sent `"temperature": 0.2`. Claude Opus 4 (`claude-opus-4-7`) uses extended thinking internally and requires temperature = 1. Any other value causes 400 Bad Request.

**Fix:** Removed `temperature` from the payload entirely (API defaults to 1).

**File:** `blackbox_service/agent.py`

### 1b. Error Body Was Swallowed
**What was broken:** `response.raise_for_status()` raised the HTTP status but not Anthropic's error body. Dashboard showed "400 Bad Request" with no actionable detail.

**Fix:** Wrapped `raise_for_status()` in try-except to capture and re-raise with full response body:
```python
except httpx.HTTPStatusError as exc:
    body = exc.response.text
    raise RuntimeError(f"Anthropic API {exc.response.status_code}: {body}") from exc
```

**File:** `blackbox_service/agent.py`

### 1c. `uv run lean_agent` Was Broken
`lean_agent` was missing from `[project.scripts]` in `pyproject.toml`. Fix: added `lean_agent = "blackbox_service.main:main"` and ran `uv sync`.

### 1d. Playwright Navigation Error Handling
`PlaywrightRuntime.open_tab()` called `page.goto()` with no timeout or error handling. If the target URL was unreachable, this crashed the entire `POST /runs` request. Fix: wrapped in try-except with 15s timeout — browser still opens even if initial navigation fails.

**File:** `blackbox_service/runtime.py`

### 1e. JavaScript Syntax Errors in Dashboard
The dashboard HTML is generated via Python f-string (`f"""..."""`). Python escape sequences (`\'`, `\n`) were being consumed before reaching JavaScript:
- `\'` → Python outputs `'` → JS string terminates prematurely → `Unexpected identifier 'data'`
- `\n` in single-quoted JS strings → Python outputs literal newline → `Invalid or unexpected token`

**Fixes:** Changed `\'` → `\\'` (two occurrences in IMPACT object), `'\n'` → `'\\n'` (two occurrences in buildReport).

**File:** `blackbox_service/api.py`

---

## 2. Model Configuration

| Setting | Original | New |
|---------|----------|-----|
| Default model | `claude-opus-4-7` | `claude-opus-4-7` (kept — strongest available, now works after temp fix) |
| Settings.py fallback | `claude-opus-4-7` | `claude-sonnet-4-6` (safer fallback if opus unavailable) |
| Gemini fallback | `gemini-2.5-flash` | `gemini-2.5-flash` (unchanged) |

The `ANTHROPIC_API_KEY` in `.env` is read file-only (never from shell env) to prevent accidental exposure.

---

## 3. Dashboard UX — Complete Overhaul

### 3a. Single LAUNCH Button (Original: 3 clicks)
Original required: ① Start Run → ② Connect → ③ Start Agent

New: one **LAUNCH** button that atomically:
1. `POST /runs` → creates run, opens Playwright browser
2. Immediately opens `EventSource` on `/runs/{id}/stream` (no separate Connect)
3. Immediately `POST /runs/{id}/agent/start`
4. Updates header with run ID and "scanning…" status

### 3b. Model Badge
`app.state.anthropic_model` is now stored in `create_app()` and rendered into the HTML template: `🤖 claude-opus-4-7`

### 3c. Live Findings Panel
Right column now has a **LIVE FINDINGS** section. Each `agent.reasoning` SSE event triggers `tryExtractFinding()` in JavaScript which scans `hypothesis + thought` text against 10 vulnerability regex patterns:

```js
const VULN_PATTERNS = [
  { re: /sqli|sql inject|OR 1=1|1=1--|union select/i, type:'SQL Injection', cwe:'CWE-89', sev:'critical', cvss:'9.8' },
  { re: /xss|cross.site.script|onerror=alert/i,       type:'XSS',           cwe:'CWE-79', sev:'high',     cvss:'7.2' },
  { re: /idor|insecure direct object/i,               type:'IDOR',          cwe:'CWE-284', sev:'high',    cvss:'7.5' },
  { re: /auth.bypass|bypassed auth/i,                 type:'Auth Bypass',   cwe:'CWE-287', sev:'critical', cvss:'9.1' },
  { re: /missing.auth|unauthenticated.*api/i,         type:'Missing API Auth', cwe:'CWE-306', sev:'high',  cvss:'7.5' },
  { re: /admin.*accessible|admin.*bypass/i,           type:'Broken Access Control', cwe:'CWE-285', sev:'high', cvss:'8.1' },
  { re: /jwt.*found|token.*localStorage/i,            type:'Sensitive Data Exposure', cwe:'CWE-200', sev:'medium', cvss:'5.3' },
  { re: /command.inject|rce/i,                        type:'RCE',           cwe:'CWE-78', sev:'critical', cvss:'10.0' },
  { re: /path.travers/i,                              type:'Path Traversal', cwe:'CWE-22', sev:'high',    cvss:'7.5' },
  { re: /ssrf/i,                                      type:'SSRF',          cwe:'CWE-918', sev:'high',    cvss:'8.6' },
];
```

Findings are deduplicated by type. The match fires AFTER `agent.step.completed` (so the result_preview is available as evidence).

### 3d. Full Professional Pentest Report
On `agent.finished`, the "View Report" button becomes active (with pulse animation). The report is generated entirely in JavaScript from in-memory state (`allSteps[]`, `allFindings[]`):

- **Target, date, duration, model, risk rating** (highest severity found)
- **Executive summary** — auto-generated text based on finding counts
- **Vulnerability summary table** — sorted critical→high→medium→low
- **Per-finding**: CWE, CVSS score, description (from Claude's reasoning), Proof of Concept (hypothesis text), evidence snippet (result_preview), hardcoded impact + remediation
- **Attack timeline** — key steps (fill, click, eval_js, navigate) with Claude's reasoning
- **Copy** and **Print** buttons, `R` keyboard shortcut, click-outside to close

### 3e. Cache-Control + Root Redirect
- `GET /dashboard` now returns `Response` with `Cache-Control: no-store, no-cache` — browser never serves stale HTML
- `GET /` now redirects to `/dashboard` (was 404)

---

## 4. Main Agent Loop — How It Actually Works

The main dashboard uses `AnthropicPlanner` in `blackbox_service/agent.py` via the `/runs` API. This is the simplest, fastest path.

### The Step Loop (`service.py: run_agent_steps`)
```
for step_index in range(max_steps):
    1. _build_agent_context(run_id, step_index, max_steps)
    2. planner.next_decision(context) → AgentDecision
    3. _normalize_decision(run_id, decision)  # fill missing tab_id, url, script
    4. _emit_agent_reasoning(...)  # 3 SSE events: agent.thought, agent.hypothesis, agent.reasoning
    5. execute_action(run_id, decision.action_type, decision.params)
    6. emit agent.step.completed
    7. if decision.done: break
    8. sleep(step_delay_ms / 1000)
```

### What Claude Sees Each Step
```json
{
  "run": { "run_id", "status", "targets", "options", "active_tab_id" },
  "tabs": [{ "tab_id", "url", "title", "is_active" }],
  "active_tab_id": "tab-xxxx",
  "page_content": {
    "url": "http://localhost:3000/#/login",
    "title": "OWASP Juice Shop",
    "text": "visible text of the page (up to 4000 chars)",
    "inputs": [{ "tag", "type", "name", "id", "placeholder" }],
    "links": ["http://...", "..."]
  },
  "recent_events": ["last 12 events from SQLite"],
  "step_index": 3,
  "max_steps": 20,
  "allowed_actions": ["click", "fill", "navigate", "eval_js", ...]
}
```

### What Claude Returns
```json
{
  "thought": "I see a login form. The email field is likely vulnerable to SQL injection.",
  "hypothesis": "SQLi ' OR 1=1-- in email field may bypass authentication",
  "action_type": "fill",
  "params": { "tab_id": "tab-abc123", "selector": "#email", "value": "' OR 1=1--" },
  "done": false
}
```

### Planner Failover
`FailoverPlanner` wraps `AnthropicPlanner` (primary) and `GeminiPlanner` (backup). On any exception from Anthropic (rate limit, 500, network error), it transparently switches to Gemini for that step and all subsequent steps.

---

## 5. Three-Phase Engagement Pipeline — Deep Dive

The engagement pipeline (`/engagements` API) runs three specialized agents sequentially, each with its own LLM-driven loop.

### Phase Architecture
```
Target URL
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 1: DiscoveryAgent                                  │
│   Input: target_url                                      │
│   Loop: Claude decides to probe endpoints, check robots  │
│         /admin, /api, /login, follow links, etc.         │
│   Output: { hosts[], endpoints[], tech_stack[] }         │
└──────────────────────────┬──────────────────────────────┘
                           │ ctx.state["discovery_endpoints"]
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 2: AccessTestAgent                                 │
│   Input: target_url + discovered endpoints               │
│   Loop: Claude tests auth, API probing, IDOR, admin      │
│   Output: { auth_status, suspected_findings[] }          │
└──────────────────────────┬──────────────────────────────┘
                           │ (optional approval gate)
                           │ ctx.state["suspected_findings"]
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 3: ConfirmEvidenceAgent                            │
│   Input: suspected_findings                              │
│   Loop: Claude re-tests each finding, captures evidence  │
│   Output: { confirmed_findings[], false_positives[] }    │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
                    Executive Report
```

### The Approval Gate
If `approval_mode = "mandatory"` OR (`"optional"` and findings exist):
1. Orchestrator pauses → status = `"paused_for_approval"`
2. Human reviews suspected findings via dashboard
3. `POST /engagements/{id}/approval` with `{"approved": true/false}`
4. If approved → ConfirmEvidence phase starts in a new thread
5. If rejected → report generated with unconfirmed findings, engagement marked complete

### Budget Tracking
```python
BudgetState(limit_usd=50.0, warn_threshold=0.8, pause_threshold=0.95)

# After each phase:
rec.budget.spent_usd += cost
ratio = spent / limit
if ratio >= 1.0:    → "budget_exhausted" status
elif ratio >= 0.95: → "budget.pause_threshold" event
elif ratio >= 0.80: → "budget.warn_threshold" event
```

### Event System
Every significant action appends to `EngagementRecord.events[]`:
```python
EngagementEvent(type="phase.start",    payload={"phase": "discovery"})
EngagementEvent(type="phase.end",      payload={"phase": "discovery", "endpoints": 23})
EngagementEvent(type="tier4.navigation.result", payload={"ok": True, "urls": [...]})
EngagementEvent(type="engagement.completed", payload={"confirmed": 3})
```

---

## 6. LLM Decision Loop in Agents (agents_v2)

Each agent in the three-phase pipeline uses `AgentBase._call_llm()` — a direct Anthropic API call at each step.

### _call_llm() Internals (`blackbox_service/agents_v2/base.py`)
```python
def _call_llm(self, ctx: AgentContext, system_prompt: str, user_context: dict) -> dict:
    payload = {
        "model": ctx.anthropic_model,   # e.g. "claude-opus-4-7"
        "max_tokens": 1024,
        "system": system_prompt,        # agent-specific instructions
        "messages": [{"role": "user", "content": json.dumps(user_context)}],
    }
    resp = httpx.Client(timeout=45.0).post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ctx.anthropic_api_key, "anthropic-version": "2023-06-01"},
        json=payload,
    )
    # Parse JSON from response text using regex \{.*\}
    # Fallback: if no API key or parse fails → {done: True, action_type: "none"}
```

### User Context (what Claude sees per agent)

**DiscoveryAgent:**
```json
{
  "target_url": "http://localhost:3000",
  "step": 4,
  "max_steps": 12,
  "endpoints_found": 8,
  "recent_observations": [
    {"action_type": "http_get", "ok": true, "result_preview": "status=200 robots.txt..."}
  ],
  "allowed_actions": ["http_get", "get_page_content", "navigate"]
}
```

**AccessTestAgent:**
```json
{
  "target_url": "...",
  "step": 2,
  "discovery_endpoints": ["/login", "/admin", "/api/users"],
  "suspected_so_far": 1,
  "recent_observations": [...],
  "allowed_actions": ["http_get", "navigate", "get_page_content", "ai_navigate", "snapshot"]
}
```

**ConfirmEvidenceAgent:**
```json
{
  "target_url": "...",
  "suspected_findings": [
    {"finding_id": "sf-abc123", "vuln_type": "sqli", "endpoint": "/rest/user/login",
     "severity": "critical", "evidence_snippet": "status=200 body=JWT token"}
  ],
  "confirmed_so_far": 0,
  "recent_observations": [...],
  "allowed_actions": ["http_get", "navigate", "get_page_content", "snapshot"]
}
```

---

## 7. Browser Interaction Engine (BIE) — Architecture

The BIE (`blackbox_service/bie/engine.py`) abstracts browser interaction into tiers, routing each `BIERequest` to the appropriate execution layer.

### Tier Routing
```python
_TIER1_ACTIONS = {"http_get", "http_post", "http_probe"}
_TIER2_ACTIONS = {"navigate", "click", "fill", "eval_js", "snapshot",
                  "get_page_content", "read_console", "read_network", "switch_tab", "open_tab"}
# ai_navigate → Tier 4 (LLM-driven)
```

### Tier 1 — HTTP Probing (httpx)
- Direct HTTP requests, no browser
- 10s timeout, follows redirects
- Returns: `{status_code, body_preview (800 chars), headers, url, cost_usd: 0.0001}`
- Used by: DiscoveryAgent (endpoint probing), AccessTestAgent (API testing), ConfirmEvidenceAgent

### Tier 2 — Playwright Browser (ThreadedPlaywrightRuntime)
- Real Chromium browser controlled via Playwright sync API
- Thread-safety: ALL browser calls go through a `queue.Queue` to the single browser owner thread
```python
class ThreadedPlaywrightRuntime:
    def _call(self, method_name, *args, **kwargs):
        out_q = queue.Queue(maxsize=1)
        self._tasks.put((method_name, args, kwargs, out_q))
        ok, value = out_q.get()  # blocks until browser thread completes
        if ok: return value
        raise value  # re-raise exception from browser thread
```
- Used by: main dashboard agent (all 13 browser actions)

### Tier 4 — LLM-Driven Browser (browser-use)
- Uses `browser-use` library with `ChatAnthropic` LLM
- Invoked when action_type = `"ai_navigate"` with an instruction string
- Claude autonomously clicks, fills, navigates to accomplish the instruction
- Returns: `{actions[], urls[], route_memory (last 8 URLs), final_result, cost_usd: 0.02}`
- Fail-fast mode: if LLM call fails, returns fallback route (navigate + get_page_content) at $0.005
- Used by: AccessTestAgent for complex auth flows it can't handle with simple navigate/fill

### BIERequest / BIEOutcome
```python
@dataclass
class BIERequest:
    run_id: str         # which Playwright context to use
    goal: str           # human-readable description of what we're trying to do
    action_type: str    # determines which tier handles this
    params: dict        # action-specific parameters

@dataclass
class BIEOutcome:
    ok: bool            # did the action succeed?
    tier_used: int      # 1, 2, or 4
    action_type: str
    result: Any         # action-specific result dict
    error: str | None   # error message if not ok
    cost_usd: float     # estimated cost for budget tracking
```

---

## 8. Complete Technology Stack

| Component | Library/Framework | Version | Purpose |
|-----------|------------------|---------|---------|
| **API Server** | FastAPI | ≥0.115 | HTTP API + SSE streaming endpoints |
| **ASGI Server** | Uvicorn | ≥0.30 | Serves the FastAPI app |
| **Browser Automation** | Playwright (sync_api) | ≥1.50 | Real Chromium browser control |
| **LLM Planning** | Anthropic API (direct httpx) | — | Agent decision making at each step |
| **Primary Model** | claude-opus-4-7 | — | Most capable reasoning for security testing |
| **LLM Fallback** | Google Gemini API (direct httpx) | — | Transparent failover if Anthropic fails |
| **Fallback Model** | gemini-2.5-flash | — | Fast Gemini model for fallback |
| **High-level Browser AI** | browser-use | ≥0.1.40 | Tier 4 autonomous browser navigation |
| **LLM for browser-use** | langchain-anthropic | ≥0.3.0 | ChatAnthropic wrapper for browser-use |
| **HTTP Client** | httpx | ≥0.27 | API calls + Tier 1 HTTP probing |
| **Data Models** | Pydantic v2 | ≥2.8 | Type-safe models, JSON serialization |
| **Database** | SQLite (stdlib) | — | Event history, run records, tab state |
| **Config Loading** | python-dotenv | ≥1.2 | .env file → settings dataclass |
| **Thread Safety** | threading + queue.Queue | stdlib | Playwright on dedicated owner thread |
| **Agent Coordination** | threading.Thread (daemon) | stdlib | Each engagement in background thread |
| **SSE Streaming** | asyncio + fastapi StreamingResponse | — | Real-time event push to dashboard |

---

## 9. Dashboard Real-Time Flow — Technical Detail

```
Server side (Python):                     Client side (JavaScript):

Agent loop executes step
    │
    ▼
emit EventEnvelope to RunEventBus        EventSource /runs/{id}/stream
    │                                          │
RunEventBus.subscribe() generator             │
yields event every 50ms poll              ←── SSE data frame
    │
FastAPI StreamingResponse formats it

Events and their JS handlers:
┌────────────────────┬─────────────────────────────────────────────────────┐
│ agent.reasoning    │ Create reasoning card, store in pendingEvidence{}    │
│ agent.step.        │ Update card result, call tryExtractFinding() with    │
│   completed        │ result_preview as evidence, update progress bar      │
│ artifact.          │ Load /artifacts/{run_id}/{filename} into img tag     │
│   screenshot       │                                                      │
│ agent.finished     │ Show "View Report" button (pulsing), set status      │
│ agent.failed       │ Show red error banner with full error body + hints   │
│ action.*           │ Add line to event log strip at bottom                │
└────────────────────┴─────────────────────────────────────────────────────┘
```

### Finding Detection Algorithm
```js
// Runs on EVERY agent.step.completed event
function tryExtractFinding(payload, resultPreview) {
    const text = payload.thought + ' ' + payload.hypothesis;
    for (const vp of VULN_PATTERNS) {
        if (vp.re.test(text)) {
            if (allFindings.find(f => f.type === vp.type)) return;  // deduplicate
            allFindings.push({ type, cwe, sev, cvss, hypothesis, evidence: resultPreview, step });
            renderFindingCard(finding);  // show immediately in right panel
        }
    }
}
```

---

## 10. How to Run for Demo

```bash
# Step 1: Start Juice Shop (Docker — required for local demo)
docker run -d -p 3000:3000 bkimminich/juice-shop
# Juice Shop loads at http://localhost:3000 (may take 30 seconds first run)

# Step 2: Start the Blackbox service
uv run lean_agent
# Service starts at http://localhost:8080

# Step 3: Open dashboard
open http://localhost:8080
# URL pre-filled with http://localhost:3000
# Click LAUNCH → browser opens → agent starts scanning

# Step 4: Watch the agent find SQLi, XSS, IDOR...
# When done: click "View Report" for professional pentest report

# Alternative: Direct browser-use agent (no dashboard, old path)
uv run python run_agent.py http://localhost:3000
```

### Model Configuration
The model is set in `.env`:
```env
BLACKBOX_AGENT_MODEL=claude-opus-4-7    # Strongest model, best results
BLACKBOX_BROWSER_HEADLESS=false         # Show the browser window (impressive for demos)
BLACKBOX_AGENT_MAX_STEPS=20             # Steps per scan
```

---

## 11. Test Suite

**32 tests across 14 files** — all pass. Tests use `InMemoryRuntime` and `ScriptedPlanner` (no browser, no API key needed).

Key changes from baseline:
- `test_settings.py`: Updated default model assertion (`claude-opus-4-7` → `claude-sonnet-4-6` fallback default)
- `test_access_test_agent_tier4.py`: Replaced hardcoded-behavior test with LLM-contract test
- `test_engagement_api.py`: New tests for engagement lifecycle, approval flow
- `test_access_test_agent_tier4.py`: Verifies agent returns valid summary when no API key

---

## 12. What's the Same as `explanation.md`

- All original API endpoints (`/runs`, `/runs/{id}/actions`, `/runs/{id}/stream`, etc.)
- `InMemoryRuntime` and `PlaywrightRuntime` core logic
- `BlackboxService` orchestration (start_run, execute_action, run_agent_steps)
- `RuleBasedPlanner`, `FailoverPlanner`, `GeminiPlanner` — unchanged
- `SQLiteEventStore` and `RunEventBus` — unchanged
- Dashboard URL params (`?target=...&autorun=1&autostart_agent=1`) — still work
- `demo_blackbox` CLI command — still works
- All 13 browser action types (click, fill, navigate, eval_js, etc.)
