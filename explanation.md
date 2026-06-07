# Blackbox Agent Service — Complete Technical Reference

> Last updated: 2026-04-20  
> Use this document as the single source of truth when making any change — small or large.

---

## 1. What This System Is

A **browser automation service** that acts as the "hands and senses" for an AI security testing agent. It:

- Opens a real Chromium browser (via Playwright) pointed at a target web app
- Gives an AI planner (Claude Opus with Gemini fallback) a set of browser actions to execute
- Streams the agent's reasoning (thought + hypothesis + action) live to a web dashboard via Server-Sent Events (SSE)
- Stores every event in SQLite for memory and replay
- Exposes a stable HTTP API so your team's external agent can replace the built-in Claude planner at any time

**Two separate execution paths:**
1. **`blackbox_service` path** — full service (FastAPI + SQLite + SSE dashboard + planners). Controlled via HTTP API or demo CLI.
2. **`run_agent.py` path** — direct `browser-use` agent runner, bypasses the service entirely, uses LangChain + Chromium directly with a browser sidebar overlay.

**For demo:** Claude Opus acts as the "brain". Your team's future agent plugs in via the same HTTP API.

---

## 2. How to Run Juice Shop on Port 3000

### Option A — Docker (recommended, one command)
```bash
docker run -d --name juice-shop -p 3000:3000 bkimminich/juice-shop
```
Juice Shop is now at `http://localhost:3000`. Stop it with `docker stop juice-shop`.

### Option B — Node.js (if Docker unavailable)
```bash
git clone https://github.com/juice-shop/juice-shop.git
cd juice-shop
npm install
npm start
# Runs at http://localhost:3000
```

### Option C — Heroku public instance (no install needed)
Use `https://juice-shop.herokuapp.com` as the target URL (requires internet; may be slow).

---

## 3. How to Run the Full Demo

**Prerequisites:** Juice Shop running on port 3000, `ANTHROPIC_API_KEY` (and optionally `GEMINI_API_KEY`) in `.env`.

```bash
# One command — opens browser + dashboard automatically
uv run demo_blackbox http://localhost:3000
```

What happens in sequence:
1. `demo.py` checks if the service is running on port 8080; if not, spawns it
2. Waits up to 30 seconds for `/health` to respond
3. Opens your system browser at `http://localhost:8080/dashboard?target=http://localhost:3000&autorun=1&autostart_agent=1`
4. Dashboard auto-creates a run and starts the agent loop
5. A visible Chromium window opens on Juice Shop
6. Agent reasoning cards appear in the dashboard as Claude thinks and acts

**To run without auto-starting the agent** (manual control):
```bash
uv run demo_blackbox http://localhost:3000 --no-autostart-agent
```

**To run without opening your browser** (headless dashboard):
```bash
uv run demo_blackbox http://localhost:3000 --no-browser
```

**To start the service only** (no demo, pure API server):
```bash
uv run lean_agent
# or (identical — both map to blackbox_service.main:main)
uv run blackbox-agent
# or
uv run python -m blackbox_service.main
```

**To run the browser-use direct agent** (no service, no dashboard):
```bash
uv run python run_agent.py http://localhost:3000
```

---

## 4. Project Directory Structure

```
blackbox-agent/
├── .env                          # Your actual secrets — API key goes here
├── .env.example                  # Template for .env — all variables with defaults
├── lean_agent.py                 # Thin entry point → calls blackbox_service.main:main()
├── run_agent.py                  # Alternative entry point → runs browser_use_agent directly
├── pyproject.toml                # uv/pip package config, defines console scripts
├── explanation.md                # This file
├── README.md                     # Quick-start docs
├── architecture.mmd              # Mermaid architecture diagram
├── user_flow.mmd                 # Mermaid user-flow diagram
├── blackbox_events.db            # SQLite database (auto-created on first run)
├── artifacts/                    # Screenshots saved here: artifacts/{run_id}/{tab_id}-{hex}.png
├── agents/                       # Pluggable agent implementations
│   ├── browser_use_agent.py      # browser-use + LangChain agent (Claude + Gemini LLM)
│   └── display.py                # Browser sidebar overlay + Eruda DevTools injector
├── tests/                        # 26 unit tests across 13 files
│   ├── test_agent_reasoning.py
│   ├── test_actions_and_memory.py
│   ├── test_api_contracts.py
│   ├── test_browser_use_agent_fallback.py
│   ├── test_demo_blackbox.py
│   ├── test_event_store.py
│   ├── test_explanation_doc.py
│   ├── test_http_client.py
│   ├── test_planner_fallback.py
│   ├── test_runtime_tabs.py
│   ├── test_service_fallback.py
│   ├── test_settings.py
│   └── test_streaming.py
└── blackbox_service/             # Main package
    ├── __init__.py               # Exports BlackboxService + create_app
    ├── asgi.py                   # ASGI entry for uvicorn
    ├── main.py                   # Service bootstrap + uvicorn exec
    ├── settings.py               # Config loader from .env
    ├── models.py                 # Pydantic data models
    ├── runtime.py                # Browser abstraction (InMemory + Playwright)
    ├── agent.py                  # Planner implementations + ALLOWED_ACTIONS
    ├── service.py                # Core orchestration logic (BlackboxService)
    ├── stream.py                 # In-memory SSE event bus (RunEventBus)
    ├── store.py                  # SQLite event persistence (SQLiteEventStore)
    ├── api.py                    # FastAPI app + embedded dashboard HTML
    ├── client.py                 # HTTP client for external orchestrators
    └── demo.py                   # Demo launcher script
```

---

## 5. File-by-File Reference

### `lean_agent.py`
**What it is:** A 2-line entry point.  
**What it does:** Calls `blackbox_service.main:main()`.  
**When to change:** Never — it's just the registered CLI command for `uv run lean_agent`.

---

### `run_agent.py`
**What it is:** Alternative entry point that bypasses the full service stack.  
**What it does:** Imports `agents.browser_use_agent.run()` and calls it with `asyncio.run()`. Takes target URL as a positional CLI argument.  
**Key feature:** Agent implementation is swappable by changing the import on line 20.  
**When to change:** When you want to swap which `agents/` module is the active runner.

---

### `agents/browser_use_agent.py`
**What it is:** A self-contained penetration-testing agent using the `browser-use` library with LangChain LLMs.  
**What it does:**
- Phase 0: Injects network + console instrumentation into the target page
- Phase 1: Reconnaissance — reads page content, console logs, network requests
- Phase 2+: Exploitation — attempts XSS, SQLi, IDOR, auth bypass
- Uses `ChatAnthropic` (Claude) as primary LLM, falls back to `ChatGoogle` (Gemini) on `ModelProviderError` / `ModelRateLimitError`
- Calls `display.py` to inject sidebar overlay and Eruda DevTools into the browser window

**Key export:** `async def run(url: str) -> None`  
**When to change:** To change the agent's security testing methodology, LLM backend, or add new attack phases.

---

### `agents/display.py`
**What it is:** Browser overlay system (~400 lines) that injects a live sidebar into the Chromium window.  
**What it does:**
- Injects a `position:fixed` sidebar panel showing agent step reasoning in real time
- Displays different border colors by state: thinking (blue), exploiting (red), success (green), idle (grey)
- Injects Eruda DevTools panel for Console and Network tab visibility
- Strips ANSI codes, HTML-escapes all injected content
- Provides animated glow borders via CSS keyframes

**When to change:** To change the visual appearance of the in-browser overlay or add new state indicators.

---

### `blackbox_service/settings.py`
**What it is:** Configuration loader.  
**Key class:** `BlackboxSettings` (dataclass with all config fields).  
**Key function:** `load_settings(env_file=".env")` — reads `.env` file then falls back to env vars.

**All configuration fields:**

| Field | Env Variable | Default | Purpose |
|-------|-------------|---------|---------|
| `host` | `BLACKBOX_HOST` | `0.0.0.0` | Server bind address |
| `port` | `BLACKBOX_PORT` | `8080` | Server port |
| `db_path` | `BLACKBOX_DB_PATH` | `blackbox_events.db` | SQLite file path |
| `use_playwright` | `BLACKBOX_USE_PLAYWRIGHT` | `true` | Enable real browser |
| `browser_headless` | `BLACKBOX_BROWSER_HEADLESS` | `false` | Hide browser window |
| `default_target_url` | `BLACKBOX_TARGET_URL` | `http://127.0.0.1:3000/#/` | Pre-filled URL in dashboard |
| `agent_model` | `BLACKBOX_AGENT_MODEL` | `claude-opus-4-7` | Claude model for planning |
| `gemini_model` | `BLACKBOX_GEMINI_MODEL` | `gemini-2.5-flash` | Gemini fallback model |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | `""` | **File-only** — never read from shell env |
| `gemini_api_key` | `GEMINI_API_KEY` / `GOOGLE_API_KEY` | `""` | **File-only** — Gemini fallback key |
| `agent_max_steps` | `BLACKBOX_AGENT_MAX_STEPS` | `20` | Max steps per agent loop |
| `agent_step_delay_ms` | `BLACKBOX_AGENT_STEP_DELAY_MS` | `1000` | Pause between steps (ms) |
| `auto_open_browser` | `BLACKBOX_AUTO_OPEN_BROWSER` | `true` | Open dashboard on demo start |

**Security note:** `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` are intentionally loaded **only** from the `.env` file, never from the shell environment. This prevents accidental key exposure via `env` dumps.

**When to change:**
- To change model: edit `BLACKBOX_AGENT_MODEL` or `BLACKBOX_GEMINI_MODEL` in `.env`
- To add a new config field: add it to `BlackboxSettings` dataclass AND to `load_settings()` function
- To change defaults: edit both the dataclass default AND the `_to_int`/`_to_bool` default strings in `load_settings()`

---

### `blackbox_service/models.py`
**What it is:** Pydantic data models shared across the whole system.

| Model | Purpose |
|-------|---------|
| `EventEnvelope` | Every event stored/streamed — has `event_id`, `run_id`, `ts`, `type`, `tab_id`, `step_id`, `payload`, `token_cost` |
| `TabState` | State of one browser tab — `run_id`, `tab_id`, `url`, `title`, `parent_tab_id`, `correlation_id`, `is_active`, `opened_at` |
| `RunRecord` | A test run — `run_id`, `status`, `created_at`, `updated_at`, `targets`, `options`, `active_tab_id`, `error` |
| `StartRunRequest` | POST body for `/runs` — `targets: list[str]`, `options: dict` |
| `StartRunResponse` | Response for run creation |
| `ActionRequest` | POST body for `/runs/{id}/actions` — `action_type`, `params` |
| `ActionResponse` | Response for action execution — `ok`, `action_type`, `result` |
| `AgentStartRequest` | POST body for `/runs/{id}/agent/start` — `max_steps` (default **8**), `step_delay_ms` (default **400**) |

**When to change:** When you need a new field in any API request/response. Add it here first.

---

### `blackbox_service/runtime.py`
**What it is:** Browser abstraction layer. Two implementations with identical method signatures.

#### `InMemoryRuntime`
- No real browser — fake tabs, no JS execution (simple arithmetic only)
- Used by all 27 unit tests (fast, no dependencies)
- `get_page_content` returns `{"url": ..., "title": ..., "text": "(offline)", "inputs": [], "links": []}`
- Screenshots write a text placeholder file

#### `PlaywrightRuntime`
- Real Chromium browser via Playwright `sync_api`
- One `BrowserContext` per run, one `Page` per tab
- All page calls happen on the thread that created the runtime (thread-safety constraint)
- Attaches console + network listeners on tab open (`_on_console`, `_on_request`, `_on_response`)

**All methods (same signature on both classes):**

| Method | Params | What it does |
|--------|--------|-------------|
| `start_run(run_id, targets)` | `str, list[str]` | Opens first tab at `targets[0]`, returns `tab_id` |
| `stop_run(run_id)` | `str` | Closes browser context for that run |
| `open_tab(run_id, url, correlation_id, parent_tab_id)` | — | Opens new tab, navigates to url |
| `switch_tab(run_id, tab_id)` | — | Sets active tab |
| `navigate_tab(run_id, tab_id, url)` | — | Navigates existing tab |
| `eval_js(run_id, tab_id, script)` | — | Runs JS, returns result |
| `inject_html(run_id, tab_id, html)` | — | Appends HTML to body |
| `get_console_logs(run_id, tab_id)` | — | Returns `[{type, text}]` |
| `get_network_events(run_id, tab_id)` | — | Returns `[{kind, method/status, url}]` |
| `list_tabs(run_id)` | — | Returns `list[TabState]` |
| `get_active_tab(run_id)` | — | Returns active `tab_id` or `None` |
| `click(run_id, tab_id, selector)` | CSS selector | Clicks element; returns `{ok, selector}` or `{ok:False, error}` |
| `fill(run_id, tab_id, selector, value)` | CSS selector, string | Clears + types into input field |
| `select_option(run_id, tab_id, selector, value)` | CSS selector, option value | Selects `<select>` dropdown option |
| `wait_for_selector(run_id, tab_id, selector, timeout_ms)` | CSS selector, int | Waits for element; returns `{ok, found: bool}` |
| `get_page_content(run_id, tab_id, max_chars)` | — | Returns `{url, title, text, inputs[], links[]}` |
| `capture_screenshot(run_id, tab_id, artifact_name)` | — | Saves PNG to `artifacts/{run_id}/{artifact_name}` |

**When to change:**
- To add a new browser action (e.g., `hover`, `press_key`): add the method here to BOTH classes, then add a handler in `service.py execute_action()`, then add the action name to `ALLOWED_ACTIONS` in `agent.py`
- All 3 files must be updated together for a new action

**Thread safety:** `PlaywrightRuntime` is initialized in a worker thread inside `_create_playwright_runtime()` in `service.py`. All subsequent page calls happen from the agent-loop thread (`agent-loop-{run_id}`). Do NOT call runtime methods from multiple threads simultaneously for the same run.

---

### `blackbox_service/agent.py`
**What it is:** Agent planning logic. Defines what decisions the agent can make.

**Key constant:**
```python
ALLOWED_ACTIONS = {
    "open_tab", "switch_tab", "navigate", "eval_js", "inject_html",
    "read_console", "read_network", "snapshot",
    "click", "fill", "select_option", "wait_for_selector", "get_page_content",
    "none",
}
```

**`AgentDecision` dataclass:**
```python
@dataclass
class AgentDecision:
    thought: str       # Agent's full reasoning text
    hypothesis: str    # Specific vulnerability being tested
    action_type: str   # Must be in ALLOWED_ACTIONS
    params: dict       # Action parameters (tab_id, selector, value, etc.)
    done: bool = False # True = stop the agent loop
```

**Five planners:**

| Planner | Used when | Behavior |
|---------|-----------|---------|
| `RuleBasedPlanner` | No API key in `.env` | 4-step hardcoded: read_console → read_network → eval_js → snapshot |
| `AnthropicPlanner` | Anthropic API key present | Calls Claude API with full context; returns JSON decision |
| `GeminiPlanner` | Gemini API key present | Calls Google Gemini API with same security prompt; returns JSON decision |
| `FailoverPlanner` | Both keys present | Wraps AnthropicPlanner (primary) + GeminiPlanner (fallback); switches automatically on any exception |
| `ScriptedPlanner` | Tests only | Replays a pre-written list of decisions |

**`AnthropicPlanner` details:**
- Endpoint: `https://api.anthropic.com/v1/messages`
- Model: read from `settings.agent_model` (default `claude-opus-4-7`)
- Max tokens: `1024`, Temperature: `0.2`, Timeout: `45 seconds`
- System prompt: Security-focused, instructs Claude to attempt XSS, SQLi, IDOR, auth bypass

**`GeminiPlanner` details:**
- Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
- Model: read from `settings.gemini_model` (default `gemini-2.5-flash`)
- Same security testing system prompt as AnthropicPlanner

**`build_planner(anthropic_key, anthropic_model, gemini_key, gemini_model)` — Factory function:**
- Both keys present → `FailoverPlanner(AnthropicPlanner, GeminiPlanner)`
- Only Anthropic key → `AnthropicPlanner`
- Only Gemini key → `GeminiPlanner`
- No keys → `RuleBasedPlanner`

**When to change:**
- To change Claude's behavior/strategy: edit the `system_prompt` string in `AnthropicPlanner.next_decision()`
- To add a new action to the allowed set: add to `ALLOWED_ACTIONS` here + add method to `runtime.py` + add handler to `service.py`
- To plug in a different AI provider: create a new class implementing the `Planner` protocol (`next_decision(context) → AgentDecision`) and pass it to `create_app(planner=...)`

---

### `blackbox_service/service.py`
**What it is:** The core orchestration layer. Everything flows through `BlackboxService`.

**Constructor:**
```python
BlackboxService(
    db_path="blackbox_events.db",
    use_playwright=False,
    browser_headless=False,
    planner=None,          # Optional — defaults to RuleBasedPlanner
    artifacts_dir="artifacts",
)
```

**Key public methods:**

| Method | What it does |
|--------|-------------|
| `start_run(targets, options)` | Creates run in DB, opens browser, returns `RunRecord` |
| `stop_run(run_id)` | Closes browser context, sets status=stopped |
| `start_agent(run_id, max_steps, step_delay_ms)` | Spawns agent loop in daemon thread, returns immediately |
| `get_agent_state(run_id)` | Returns `{status, steps_completed, max_steps, last_error}` |
| `run_agent_steps(run_id, max_steps, step_delay_ms)` | The actual agent loop — called in daemon thread |
| `execute_action(run_id, action_type, params)` | Dispatches one browser action, emits event |
| `list_tabs(run_id)` | Returns tabs from SQLite |
| `list_memory(run_id, limit)` | Returns events from SQLite |
| `list_artifacts(run_id)` | Returns screenshot events |
| `stream_events(run_id)` | Returns async generator for SSE |

**Agent loop flow** (`run_agent_steps`):
```
for step_index in range(max_steps):
    context = _build_agent_context(run_id, step_index, max_steps)
    decision = planner.next_decision(context)         # Claude / Gemini API call
    normalized = _normalize_decision(run_id, decision) # Fill in missing tab_id etc.
    _emit_agent_reasoning(...)                         # Emits agent.thought + agent.hypothesis + agent.reasoning
    execute_action(run_id, decision.action_type, ...)  # Runs browser action
    emit agent.step.completed
    if decision.done: break
    sleep(step_delay_ms / 1000)
```

**`_build_agent_context` — what Claude/Gemini sees:**
```json
{
  "run": { "run_id", "status", "targets", "options", "active_tab_id" },
  "tabs": [ { "tab_id", "url", "title", "is_active" } ],
  "active_tab_id": "tab-xxxx",
  "page_content": {
    "url": "http://localhost:3000/#/",
    "title": "OWASP Juice Shop",
    "text": "visible text of the page (up to 4000 chars)",
    "inputs": [ { "tag", "type", "name", "id", "placeholder", "text" } ],
    "links": [ "http://...", "..." ]
  },
  "recent_events": [ "last 12 events from SQLite" ],
  "step_index": 3,
  "max_steps": 20,
  "allowed_actions": [ "click", "fill", "..." ]
}
```

**`_normalize_decision` — safety net for missing params:**
- If `tab_id` is missing from params → injects the current active tab ID
- If `url` is missing for navigate/open_tab → injects target URL from run
- If `script` is missing for eval_js → injects default DOM inspection script
- If still no `tab_id` and action requires one → converts to `action_type="none", done=True`

**`_emit_agent_reasoning` — three events emitted per step:**
1. `agent.thought` — `{step_index, text}` (backward compat)
2. `agent.hypothesis` — `{step_index, text}` (backward compat)
3. `agent.reasoning` — `{step_index, thought, hypothesis, action_type, params}` (used by dashboard cards)

**When to change:**
- To change how many events the planner sees as context: edit `list(...)[-12:]` in `_build_agent_context`
- To add a new action handler: add an `if action_type == "new_action":` block in `execute_action()` before the final `raise ValueError`
- To change what's auto-filled when Claude omits params: edit `_normalize_decision()`

---

### `blackbox_service/stream.py`
**What it is:** In-memory event bus for live SSE streaming.  
**Key class:** `RunEventBus`

- `publish(event)` — appends event to in-memory list for that run_id (thread-safe with lock)
- `subscribe(run_id)` — async generator that polls every 50ms and yields new events
- `snapshot(run_id)` — returns all events emitted so far for a run

**Important:** The bus is **in-memory only**. If the server restarts, live SSE clients lose their stream. Historical events are in SQLite (served via `/runs/{id}/memory`).

**When to change:** Only if you need to change polling interval (currently 50ms) or add WebSocket support.

---

### `blackbox_service/store.py`
**What it is:** SQLite persistence layer.  
**Key class:** `SQLiteEventStore`

Stores three types of data:
- **Events** (`events` table): full history — `event_id`, `run_id`, `ts`, `type`, `tab_id`, `step_id`, `payload_json`, `token_cost`
- **Runs** (`runs` table): metadata per test run
- **Tabs** (`tabs` table): current state of each browser tab

Indices: `idx_events_run_id_id`, `idx_events_run_id_type`

**Key methods:** `append_event`, `list_events`, `create_run`, `get_run`, `set_run_status`, `upsert_tab`, `list_tabs`, `set_active_tab`

**When to change:** Only if you need new query patterns (e.g., filter events by type, paginate differently).

---

### `blackbox_service/api.py`
**What it is:** FastAPI app creation + all HTTP endpoints + the embedded dashboard HTML.

**`create_app(...)` parameters:**
```python
create_app(
    db_path="blackbox_events.db",
    use_playwright=False,
    browser_headless=False,
    planner=None,
    default_target_url="http://127.0.0.1:3000/#/",
    default_agent_max_steps=20,
    default_agent_step_delay_ms=1000,
)
```

**All API endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health check → `{"status": "ok"}` |
| `POST` | `/runs` | Create run → `StartRunResponse` |
| `GET` | `/runs/{run_id}` | Get run details → `RunRecord` |
| `GET` | `/runs/{run_id}/tabs` | List tabs → `{run_id, tabs:[TabState]}` |
| `POST` | `/runs/{run_id}/actions` | Execute action → `ActionResponse` |
| `GET` | `/runs/{run_id}/memory` | List events (default limit 500) |
| `GET` | `/runs/{run_id}/artifacts` | List screenshot events |
| `POST` | `/runs/{run_id}/stop` | Stop run |
| `POST` | `/runs/{run_id}/agent/start` | Start agent loop (async, returns immediately) |
| `GET` | `/runs/{run_id}/agent/state` | Poll agent status |
| `GET` | `/runs/{run_id}/stream` | SSE event stream (persistent connection) |
| `GET` | `/artifacts/{run_id}/{filename}` | Serve PNG screenshot file |
| `GET` | `/dashboard` | Web UI dashboard (HTML) |

**Dashboard URL query params** (used by `demo.py` for auto-start):
- `?target=http://localhost:3000` — pre-fills target URL input
- `?autorun=1` — auto-creates run on page load
- `?autostart_agent=1` — auto-starts agent after run created (requires autorun=1)

**Dashboard layout:**
```
┌─ Header: target URL · Start Run · run ID · Connect · steps · delay · Start Agent · status badge ─┐
├─ Progress bar (fills as steps complete) ──────────────────────────────────────────────────────────┤
├─ Left column (main) ───────────────────────────────┬─ Right sidebar (320px) ────────────────────┤
│  Agent Reasoning panel (scrollable)                 │  Browser Tabs                              │
│  ┌─ Step N ─────────────────────────────────────┐  │  Latest Screenshot                         │
│  │ [action-type pill]                            │  │  Manual Action panel                       │
│  │ THOUGHT: full reasoning text                  │  │    (dropdown + JSON params + Execute btn)  │
│  │ Hypothesis: italic vulnerability text         │  │                                            │
│  │ Result: filled when step completes            │  │                                            │
│  └───────────────────────────────────────────────┘  │                                            │
│  [more cards...]                                    │                                            │
│  ─────────────────────────────────────────────────  │                                            │
│  Raw Events strip (120px, compact pill format)      │                                            │
└─────────────────────────────────────────────────────┴────────────────────────────────────────────┘
```

**Reasoning card colors by action type:**
- Blue left border: observe actions (`get_page_content`, `read_console`, `read_network`, `eval_js`, `snapshot`)
- Amber left border: interact actions (`click`, `fill`, `navigate`, `open_tab`, `select_option`, `wait_for_selector`)
- Red left border: probe actions (`inject_html`)
- Green left border: done (`none`)

**When to change:**
- To add a new API endpoint: add it inside `create_app()` using the `@app.get/post` decorator pattern
- To change dashboard UI: edit the large f-string in the `dashboard()` function
- To add a new action to the manual action dropdown: add an `<option>` in the `<select id="actionType">` element
- To handle a new SSE event in the dashboard: add the event name to `observedEvents` array in the JS and add a `stream.addEventListener(...)` handler

---

### `blackbox_service/main.py`
**What it is:** Service bootstrap. Called by `lean_agent.py`.

**`build_app()`:** Loads settings → calls `build_planner(anthropic_key, anthropic_model, gemini_key, gemini_model)` → calls `create_app()` → returns FastAPI app.

**`main()`:** Loads settings → uses `os.execvp()` to exec into `uvicorn blackbox_service.asgi:app` with configured host/port.

**When to change:** Only if you need to pass new initialization params to `create_app()` (e.g., a new setting field).

---

### `blackbox_service/asgi.py`
**What it is:** Single line — `app = build_app()`. Used by uvicorn as the ASGI entry point.  
**When to change:** Never.

---

### `blackbox_service/client.py`
**What it is:** HTTP client for external orchestrators (your team's agent).  
**Key class:** `BlackboxClient(base_url="http://localhost:8080")`

**Methods:**
```python
client = BlackboxClient()
run = client.create_run(targets=["http://localhost:3000"], options={})
client.run_action(run["run_id"], "get_page_content", {"tab_id": run["active_tab_id"]})
client.run_action(run["run_id"], "click", {"tab_id": "tab-xxx", "selector": "#loginButton"})
client.run_action(run["run_id"], "fill", {"tab_id": "tab-xxx", "selector": "#email", "value": "admin@juice-sh.op"})
client.list_memory(run["run_id"])
client.start_agent(run["run_id"], max_steps=8, step_delay_ms=400)
client.get_agent_state(run["run_id"])
```

**When to change:** To add convenience wrappers for new action types or new API endpoints.

---

### `blackbox_service/demo.py`
**What it is:** Demo launcher used by `uv run demo_blackbox <url>`.

**Flow:**
1. Parse CLI args: `target_url` (positional), `--no-browser`, `--no-autostart-agent`
2. Load `.env` settings
3. Check if service is running (`GET /health` on port 8080); if not, spawn it as a subprocess
4. Wait up to 30 seconds for health check to pass
5. Build dashboard URL with `?target=...&autorun=1&autostart_agent=0/1` query params
6. Open system browser at dashboard URL (unless `--no-browser`)
7. Optional: run a batch smoke test sequence if `DEMO_RUN_BATCH_SMOKE=true` in env

**When to change:** To add new CLI arguments or change the demo action sequence.

---

## 6. SSE Event Reference

All events are streamed via `GET /runs/{id}/stream`. Format:
```
event: agent.reasoning
data: {"event_id":"evt-abc","run_id":"run-xxx","ts":"...","type":"agent.reasoning","tab_id":null,"payload":{...}}
```

**Full event catalog:**

| Event type | Payload | When emitted |
|-----------|---------|-------------|
| `run.started` | `{targets, options}` | Run created, browser opened |
| `run.stopped` | `{}` | Run stopped |
| `agent.started` | `{max_steps, step_delay_ms}` | Agent loop begins |
| `agent.thought` | `{step_index, text}` | Each step — Claude's reasoning |
| `agent.hypothesis` | `{step_index, text}` | Each step — vulnerability being tested |
| `agent.reasoning` | `{step_index, thought, hypothesis, action_type, params}` | Each step — combined card event |
| `agent.step.completed` | `{step_index, action_type, done, result_preview}` | After action executes |
| `agent.finished` | `{status, steps_completed, max_steps, last_error}` | Loop ends (completed/failed) |
| `agent.failed` | `{error}` | Unhandled exception in loop |
| `action.open_tab` | `{url, parent_tab_id, correlation_id}` | Tab opened |
| `action.switch_tab` | `{}` | Tab switched |
| `action.navigate` | `{url}` | Navigation |
| `action.eval_js` | `{script, result}` | JS executed |
| `action.inject_html` | `{html_length}` | HTML injected |
| `action.click` | `{selector, result}` | Click performed |
| `action.fill` | `{selector, value}` | Input filled |
| `action.select_option` | `{selector, value}` | Dropdown selected |
| `action.wait_for_selector` | `{selector, found}` | Wait completed |
| `observation.console` | `{count}` | Console logs read |
| `observation.network` | `{count}` | Network events read |
| `observation.page_content` | `{url, input_count}` | Page content read |
| `artifact.screenshot` | `{kind, tab_id, path}` | Screenshot saved |

---

## 7. How to Plug In Your Team's Agent

The service exposes a **stable HTTP API**. Your team's agent replaces the internal Claude planner by using `BlackboxClient` directly.

### Option A — External HTTP client (no code changes needed)
```python
from blackbox_service.client import BlackboxClient

client = BlackboxClient(base_url="http://localhost:8080")

# Your agent creates a run
run = client.create_run(targets=["http://localhost:3000"], options={})
run_id = run["run_id"]
tab_id = run["active_tab_id"]

# Your agent drives the browser
client.run_action(run_id, "get_page_content", {"tab_id": tab_id})
client.run_action(run_id, "navigate", {"tab_id": tab_id, "url": "http://localhost:3000/#/login"})
client.run_action(run_id, "fill", {"tab_id": tab_id, "selector": "#email", "value": "' OR 1=1--"})
client.run_action(run_id, "click", {"tab_id": tab_id, "selector": "button[type=submit]"})

# Your agent reads back what happened
memory = client.list_memory(run_id)
```

Your agent's reasoning is NOT streamed to the dashboard in this mode. The dashboard will show browser actions but no reasoning cards.

### Option B — Custom Planner class (reasoning streams to dashboard)
```python
# In your agent code
from blackbox_service.agent import AgentDecision, Planner

class MyTeamPlanner:
    def next_decision(self, context: dict) -> AgentDecision:
        # context contains: run, tabs, active_tab_id, page_content, recent_events, step_index
        return AgentDecision(
            thought="I see a login form. Testing SQL injection.",
            hypothesis="The login endpoint may be vulnerable to SQLi",
            action_type="fill",
            params={"selector": "#email", "value": "' OR 1=1--"},
            done=False,
        )

# Wire it in blackbox_service/main.py build_app():
planner = MyTeamPlanner()
return create_app(..., planner=planner)
```

This mode streams reasoning cards to the dashboard automatically.

---

## 8. Configuration Quick-Reference

### Minimum `.env` for live operation (Claude only):
```env
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
BLACKBOX_TARGET_URL=http://localhost:3000
```

### Minimum `.env` for failover operation (Claude + Gemini):
```env
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
GEMINI_API_KEY=AIza...
BLACKBOX_TARGET_URL=http://localhost:3000
```

### Full `.env` with all options:
```env
# Server
BLACKBOX_HOST=0.0.0.0
BLACKBOX_PORT=8080
BLACKBOX_DB_PATH=blackbox_events.db

# Browser
BLACKBOX_USE_PLAYWRIGHT=true
BLACKBOX_BROWSER_HEADLESS=false    # false = visible browser window for demos

# Agent
BLACKBOX_TARGET_URL=http://127.0.0.1:3000/#/
BLACKBOX_AGENT_MAX_STEPS=20
BLACKBOX_AGENT_STEP_DELAY_MS=1000

# Models
BLACKBOX_AGENT_MODEL=claude-opus-4-7
BLACKBOX_GEMINI_MODEL=gemini-2.5-flash

# API keys (file-only — do not set in shell environment)
ANTHROPIC_API_KEY=sk-ant-api03-...
GEMINI_API_KEY=AIza...
```

---

## 9. Common Changes Reference

| What you want to do | File to change | What to change |
|--------------------|----------------|----------------|
| Change Claude model | `.env` | `BLACKBOX_AGENT_MODEL=claude-opus-4-7` |
| Change Gemini fallback model | `.env` | `BLACKBOX_GEMINI_MODEL=gemini-2.5-flash` |
| Change max steps | `.env` | `BLACKBOX_AGENT_MAX_STEPS=30` |
| Change step speed | `.env` | `BLACKBOX_AGENT_STEP_DELAY_MS=500` |
| Change Claude's security strategy | `agent.py` | Edit `system_prompt` in `AnthropicPlanner.next_decision` |
| Add new browser action (e.g. hover) | `runtime.py` + `service.py` + `agent.py` | 1) Add method to both runtimes 2) Add handler in `execute_action()` 3) Add to `ALLOWED_ACTIONS` |
| Change what Claude sees as context | `service.py` | Edit `_build_agent_context()` |
| Change dashboard layout/colors | `api.py` | Edit the HTML f-string in `dashboard()` function |
| Add new API endpoint | `api.py` | Add `@app.get/post(...)` inside `create_app()` |
| Plug in team's agent | `blackbox_service/main.py` | Replace `build_planner()` call with custom planner instance in `build_app()` |
| Use service headlessly (no browser UI) | `.env` | `BLACKBOX_BROWSER_HEADLESS=true` |
| Run tests | terminal | `uv run pytest tests/ -q` |
| Add new config field | `settings.py` | Add to `BlackboxSettings` dataclass AND `load_settings()` |
| Change browser overlay appearance | `agents/display.py` | Edit CSS/JS in `_build_sidebar_html()` |
| Change browser-use agent strategy | `agents/browser_use_agent.py` | Edit the task prompt string |

---

## 10. Run Tests

```bash
uv run pytest tests/ -q          # All 26 tests (uses InMemoryRuntime, no browser needed)
uv run pytest tests/ -v          # Verbose output with test names
uv run pytest tests/test_settings.py  # Single file
uv run pytest tests/ -k "planner"     # Tests matching keyword
```

All tests use `InMemoryRuntime` and `ScriptedPlanner` — no browser, no API key required.

**Test files (26 tests across 13 files):**

| File | Tests | What it covers |
|------|-------|----------------|
| `test_agent_reasoning.py` | 1 | ScriptedPlanner emits thought/hypothesis/observation events |
| `test_actions_and_memory.py` | 1 | eval_js, inject_html, read_console, read_network actions + event emission |
| `test_api_contracts.py` | 4 | FastAPI endpoints: create_run, fetch_run, 404 unknown run, stream, agent start/state |
| `test_browser_use_agent_fallback.py` | ~5 | browser-use agent module stubs and fallback scenarios |
| `test_demo_blackbox.py` | 2 | Demo action workflow + dashboard URL builder |
| `test_event_store.py` | 1 | SQLiteEventStore persistence and replay |
| `test_explanation_doc.py` | 1 | explanation.md exists and covers required topics |
| `test_http_client.py` | 2 | BlackboxClient HTTP operations via mocked httpx transport |
| `test_planner_fallback.py` | 2 | FailoverPlanner switches to Gemini; build_planner wraps Anthropic with Gemini fallback |
| `test_runtime_tabs.py` | 1 | Multi-tab correlation and active tab switching |
| `test_service_fallback.py` | 1 | Service fallback when Playwright initialization fails |
| `test_settings.py` | 3 | Settings loading: .env priority, Gemini key, default model |
| `test_streaming.py` | 2 | RunEventBus snapshot + async subscription with event replay |

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `agent.failed` with "Missing Anthropic API key" | `ANTHROPIC_API_KEY` not in `.env` | Add key to `.env` file (not shell env) |
| `agent.failed` with API 404 or model not found | Wrong model ID | Use `claude-opus-4-7`, `claude-sonnet-4-6`, or `claude-haiku-4-5-20251001` |
| Agent falls back to Gemini unexpectedly | Anthropic rate limit or error | Expected behavior — add `GEMINI_API_KEY` to enable graceful fallover |
| `agent.failed` with "could not launch browser" | Playwright not installed | Run `uv run playwright install chromium` |
| Browser opens but nothing loads | Juice Shop not running | `docker run -d -p 3000:3000 bkimminich/juice-shop` |
| Dashboard shows no reasoning cards | Agent not started | Click "Start Agent" or use `?autostart_agent=1` in URL |
| Service falls back to InMemoryRuntime | Playwright init failed | Check Playwright install; look for warning in server logs |
| `run_agent_steps` exits after step 0 | `_normalize_decision` found no tab | Run was created but browser tab didn't open; check Playwright |
| All actions become `"none"` | `tab_id` resolution failing | Check `_normalize_decision()` in `service.py` — active tab may be None |
| `run_agent.py` fails with import error | `browser-use` not installed | Run `uv sync` to install all dependencies |

---

## 12. All Commands Reference

### Environment Setup
```bash
# Install all dependencies
uv sync

# Install Playwright browser
uv run playwright install chromium

# Copy env template and fill in your keys
cp .env.example .env
# Then edit .env with your ANTHROPIC_API_KEY and/or GEMINI_API_KEY
```

### Start the Service (API server only)
```bash
# All three are identical — pick any
uv run lean_agent
uv run blackbox-agent
uv run python -m blackbox_service.main

# Service starts at http://localhost:8080
# Dashboard at http://localhost:8080/dashboard
# Health check: curl http://localhost:8080/health
```

### Run the Demo (service + browser + dashboard)
```bash
# Full demo — opens dashboard AND visible browser window
uv run demo_blackbox http://localhost:3000

# Skip auto-starting the agent (manual control via dashboard)
uv run demo_blackbox http://localhost:3000 --no-autostart-agent

# Skip opening your system browser (service + agent only)
uv run demo_blackbox http://localhost:3000 --no-browser

# Run batch smoke test (requires service already running)
DEMO_RUN_BATCH_SMOKE=true uv run demo_blackbox http://localhost:3000
```

### Run the browser-use Agent (direct, no service)
```bash
# Runs browser_use_agent.py directly against target
# Opens Chromium with sidebar overlay + Eruda DevTools
uv run python run_agent.py http://localhost:3000
```

### Run Tests
```bash
uv run pytest tests/ -q           # All 27 tests, quiet output
uv run pytest tests/ -v           # Verbose with test names
uv run pytest tests/test_settings.py          # Single file
uv run pytest tests/ -k "planner"             # Tests matching keyword
uv run pytest tests/ --tb=short               # Short traceback on failure
```

### Juice Shop Target
```bash
# Start (Docker)
docker run -d --name juice-shop -p 3000:3000 bkimminich/juice-shop

# Stop
docker stop juice-shop

# Check it's running
curl http://localhost:3000
```

### HTTP API (curl examples)
```bash
# Health check
curl http://localhost:8080/health

# Create a run
curl -X POST http://localhost:8080/runs \
  -H "Content-Type: application/json" \
  -d '{"targets": ["http://localhost:3000"], "options": {}}'

# Execute an action (replace RUN_ID and TAB_ID)
curl -X POST http://localhost:8080/runs/RUN_ID/actions \
  -H "Content-Type: application/json" \
  -d '{"action_type": "get_page_content", "params": {"tab_id": "TAB_ID"}}'

# Start agent loop
curl -X POST http://localhost:8080/runs/RUN_ID/agent/start \
  -H "Content-Type: application/json" \
  -d '{"max_steps": 8, "step_delay_ms": 400}'

# Poll agent state
curl http://localhost:8080/runs/RUN_ID/agent/state

# Stream events (SSE — stays open)
curl -N http://localhost:8080/runs/RUN_ID/stream

# List memory
curl http://localhost:8080/runs/RUN_ID/memory
```

---

## 13. Dependencies

**Runtime dependencies** (`pyproject.toml`):

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >=0.115.0 | HTTP API framework + SSE streaming |
| `uvicorn` | >=0.30.0 | ASGI server that runs the FastAPI app |
| `httpx` | >=0.27.0 | HTTP client for `BlackboxClient` + Anthropic/Gemini API calls |
| `playwright` | >=1.50.0 | Chromium browser automation (PlaywrightRuntime) |
| `pydantic` | >=2.8.0 | Data models and validation |
| `python-dotenv` | >=1.2.1 | `.env` file loading in `settings.py` |
| `browser-use` | >=0.1.40 | High-level browser agent framework (used by `run_agent.py` path) |
| `langchain-anthropic` | >=0.3.0 | LangChain ChatAnthropic LLM wrapper for browser-use agent |

**Dev dependencies:**

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | >=8.0.0 | Test runner for all tests |

---

## 14. ToolChannel: Governed Security Tooling via HexStrike

### Overview

The **ToolChannel** is a separate, governed channel that gives Phase-A engagement agents access to real offensive security tooling (nmap, nuclei, ffuf, gobuster, subfinder, katana, sqlmap) via HexStrike AI v6.0 — without touching or extending the BIE.

The BIE (Tier 1 httpx / Tier 2 Playwright / Tier 4 browser-use) remains exactly as it is. Agents gain a *second collaborator* — a `SecurityToolGate` — that sits alongside `self._bie`. The ToolChannel is **Phase-A only**. Phase B (`run_agent.py`, `agents/`) is intentionally excluded and has no knowledge of it.

### Architecture

```
Agent run loop
    ┌──────────────────────────┐
    │  plan_next (LLM)         │
    │        │                 │
    │   action type?           │
    │  ┌─────┴──────┐          │
    │  │            │          │
    │ BIE        ToolChannel   │
    │ action     action        │
    │  │            │          │
    └──┼────────────┼──────────┘
       ▼            ▼
  self._bie    self._invoke_tool
  .request()        │
                    ▼
            SecurityToolGate
            (scope + approval + budget + audit)
                    │
                    ▼
            HexStrikeClient
            (thin HTTP transport)
                    │
                    ▼
           HexStrike :8888
           (nmap, nuclei, ffuf, …)
```

### The Four SecurityToolGate Guardrails (updated)

Every tool call flows through `SecurityToolGate.invoke()`, which enforces these checks in order (under a threading lock for budget atomicity):

| # | Guard | Failure mode | Audit event |
|---|-------|-------------|-------------|
| 1 | **SCOPE** | `params["target"]` host must match the engagement `target_url` origin | `tool.rejected` reason=`out_of_scope` |
| 2 | **APPROVAL** | Gated tools (`sqlmap_probe`, `metasploit`, `exploit`) require `approval_granted == True` | `tool.rejected` reason=`requires_hitl_approval` |
| 3 | **BUDGET** | `tool_spent_usd + est_cost > hard_cap_usd` (separate pool — see below) | `tool.rejected` reason=`budget_exhausted` |
| 4 | **CLEANUP** | Artifact path registered in `_pending` dict before execution; removed after completion. `cleanup()` called in orchestrator `finally` block removes any orphaned pending files on completion, failure, or approval-pause | — |

**Audit event path:** all audit events (`tool.invoked`, `tool.rejected`) are routed through an `event_sink` callable injected by the orchestrator. The sink calls `orchestrator._event(rec, type, payload)` which both appends to `rec.events` and publishes to `EngagementEventBus` for live SSE delivery to the Ops Console. Without a sink (unit tests), events are appended directly.

### Gated Tools

The following tools require **HITL approval** before the gate will let them through:

| Tool | Reason |
|------|--------|
| `sqlmap_probe` | Active exploitation (SQL injection) — destructive potential |
| `metasploit` | Full exploit modules — reserved for future use |
| `exploit` | Generic exploit name guard |

No metasploit or exploit modules are wired in this release. Only `sqlmap_probe` is actively used (by `ConfirmEvidenceAgent`, post-approval).

### Tool Cost / Budget Model (separate pool)

Tool spend is tracked in a **separate pool** (`EngagementRecord.tool_spent_usd`, default 0.0) that is entirely independent of the engagement's `budget.spent_usd` (which covers LLM + browser costs). This prevents tool costs from competing with agent thinking costs.

The `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` setting (default $5.00) is the ceiling for `tool_spent_usd`. The global engagement `budget_usd` is NOT consulted by the gate. Costs are tracked atomically to prevent concurrent calls from over-spending. On a failed tool call, the reserved amount is refunded from `tool_spent_usd`.

Rough per-tool cost estimates (USD):

| Tool | Est. cost |
|------|-----------|
| nmap / nmap_scan | $0.02 |
| nuclei / nuclei_scan | $0.05 |
| ffuf | $0.02 |
| gobuster | $0.02 |
| subfinder / subfinder_enum | $0.01 |
| katana / katana_crawl | $0.03 |
| sqlmap / sqlmap_probe | $0.10 |
| (default) | $0.05 |

The `BLACKBOX_TOOL_BUDGET_HARD_CAP_USD` setting (default $5.00) is a hard upper bound that applies regardless of the engagement's `budget_usd`. Costs are tracked atomically to prevent concurrent calls from over-spending.

### Audit Trail

Every gate decision — pass or reject — appends an `EngagementEvent` to the live `EngagementRecord`. The full audit trail is available at:

- `GET /engagements/{id}/events` — all events including `tool.invoked` and `tool.rejected`
- `GET /engagements/{id}/tool-invocations` — structured `ToolInvocation` records with timing and cost

### How to Disable

HexStrike is **off by default**. When `BLACKBOX_HEXSTRIKE_ENABLED` is false/unset:

- `SecurityToolGate` is never created
- `_invoke_tool()` returns `{"ok": False, "error": "no_tool_gate"}` — no exception, no crash
- All agents fall back to BIE-only operation (identical to behavior before this feature)

To re-enable: set `BLACKBOX_HEXSTRIKE_ENABLED=true` and `BLACKBOX_HEXSTRIKE_URL=http://hexstrike:8888` (or use `docker compose up` with the provided `docker-compose.yml`).

### How to Add a New Tool

1. Add the tool name to `_TOOL_COST_MAP` in `blackbox_service/toolchannel/security_gate.py` with a cost estimate.
2. If the tool is potentially destructive, add it to `_GATED_TOOLS` (requires HITL approval).
3. Add the tool name to `_TOOL_ACTION_NAMES` on the agent class that should use it.
4. Handle the tool's output in that agent's `_after_observation` method.
5. Add the tool to `allowed_actions` in that agent's `plan_next` when `tools_enabled`.
6. Write a test that injects a canned tool result and verifies the observation is handled correctly.

### Phase B Exclusion

Phase B (`run_agent.py`, `agents/browser_use_agent.py`, `agents/display.py`) is a decoupled standalone browser-use demo. It has no orchestrator, no budget, no scope gate, and no audit log. The ToolChannel is intentionally excluded from Phase B — adding it there would bypass all governance. These files must never import `blackbox_service`.

---

## 15. Operations Console (SSE Live View)

### Overview

`GET /ops-console` serves a cinematic standalone web page that streams engagement events in real time via [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events). It is the primary demo surface for Phase A and reuses the visual language of Phase B's browser sidebar (`agents/display.py`) without injecting into the target browser.

### Static Asset Layout

```
blackbox_service/static/
  ops_console.html   # minimal semantic HTML; links CSS + JS
  ops_console.css    # dark theme, Phase-B keyframes, two-column layout
  ops_console.js     # EventSource lifecycle, EVENT_MAP, all UI updates
```

Assets are served by FastAPI `StaticFiles` mounted at `/static`. The route `GET /ops-console` reads and returns `ops_console.html`.

### SSE Stream Contract

```
GET /engagements/{engagement_id}/stream
Content-Type: text/event-stream
```

**History replay on connect:** all events already in `rec.events` are sent immediately in order so a late-joining browser sees full history.

**Live streaming:** after replay, new events are published by `EngagementOrchestrator._event()` via `EngagementEventBus` (a `threading.Queue`-per-consumer fan-out) and drained by the async generator with `asyncio.sleep(0.05)` polling.

**Stream close:** when the engagement reaches a terminal status (`completed`, `failed`, `budget_exhausted`), the generator returns, closing the SSE connection cleanly. Reconnection works normally during `paused_for_approval`.

**Consumer cleanup:** the SSE generator unsubscribes its queue in a `finally` block so no resources leak when a browser tab closes.

Each `data:` line is a JSON object:

```json
{
  "type": "tool.invoked",
  "ts": "2026-06-07T18:00:00.000Z",
  "payload": { "tool": "nmap_scan", "target": "...", "ok": true, "duration_ms": 1230, "cost_usd": 0.02 },
  "phase": "discovery",
  "status": "running",
  "budget": { "spent": 0.04, "limit": 50.0 }
}
```

The `phase`, `status`, and `budget` fields are a live snapshot from the current `EngagementRecord`, so the UI can update its chrome from any single message without an extra fetch.

### Event → Label/Color Mapping (EVENT_MAP in ops_console.js)

| Event type | Chip label | Color |
|-----------|-----------|-------|
| `phase.start`, `phase.end` | PHASE ▶ / ■ | accent (#3b9eff) |
| `tool.invoked` (ok=true) | TOOL ✓ | good (#22c55e) |
| `tool.invoked` (ok=false) | TOOL ✗ | bad (#ef4444) |
| `tool.rejected` | TOOL ✗ | bad (#ef4444) |
| `budget.warn_threshold` / `budget.pause_threshold` | BUDGET | warn (#f59e0b) |
| `budget.exhausted` | BUDGET ! | bad (#ef4444) |
| `engagement.paused_for_approval` | APPROVAL | warn — reveals Approve/Reject buttons |
| `engagement.completed` | DONE ✓ | good — triggers bb-success border |
| `engagement.failed` | FAIL | bad — triggers bb-exploit border |

Adding a new event type = one entry in `EVENT_MAP` in `ops_console.js`.

### Glow Border States

The left reasoning column carries an animated border ported verbatim from `agents/display.py` `_SIDEBAR_JS`:

| CSS class | Keyframe | Trigger |
|-----------|----------|---------|
| `.glow-think` | `bb-think` (blue pulse) | Default / discovery / access_test phases |
| `.glow-exploit` | `bb-exploit` (red pulse) | confirm_evidence / paused_for_approval / failed |
| `.glow-success` | `bb-success` (green fade) | engagement.completed |

### EngagementEventBus

New file `blackbox_service/engagement_bus.py` implements the thread-safe fan-out:

```python
class EngagementEventBus:
    def publish(self, eid: str, msg: dict) -> None: ...      # called from bg thread
    def subscribe(self, eid: str) -> queue.Queue: ...         # returns per-consumer queue
    def unsubscribe(self, eid: str, q: queue.Queue) -> None: # finally block cleanup
```

`orchestrator._event()` appends to `rec.events` (unchanged) and then calls `self._bus.publish(eid, enriched_msg)`. The bus fans out to all active subscriber queues. The existing `/events` polling endpoint and `/engagement-dashboard` are unaffected.


