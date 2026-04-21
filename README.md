# Blackbox Agent Service

Browser-capability service for webapp blackbox testing with:
- multi-tab control
- live thought/hypothesis streaming in browser
- manual + automated actions (JS/HTML/console/network/snapshot)
- SQLite event memory

## Setup

Create `.env` from [.env.example](/Users/makumar/Documents/blackbox-agent/.env.example).

Important:
- `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` are read from `.env` file values only.
- Terminal-exported keys are ignored by this service.
- Planner fallback order is `Anthropic -> Gemini -> RuleBased`.

Model defaults:
- `BLACKBOX_AGENT_MODEL=claude-opus-4-7`
- `BLACKBOX_GEMINI_MODEL=gemini-2.5-flash`

## Run Service

```bash
uv run lean_agent.py
```

or:

```bash
uv run blackbox-agent
```

## Live Demo (Browser-first)

Run with your own target URL:

```bash
uv run demo_blackbox https://example.com
```

This starts/attaches to the same live service config and opens:
- `/dashboard` with query-driven auto-run
- live event stream
- live thought/hypothesis panel
- run/tab/action controls

## API

- `POST /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/tabs`
- `POST /runs/{run_id}/actions`
- `GET /runs/{run_id}/memory`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/stream`
- `POST /runs/{run_id}/agent/start`
- `GET /runs/{run_id}/agent/state`
- `POST /runs/{run_id}/stop`
- `GET /dashboard`

## Integration

Use `blackbox_service.client.BlackboxClient` from your teammate-provided agent.

The agent remains the brain.  
This service provides browser hands/senses.

See [explanation.md](/Users/makumar/Documents/blackbox-agent/explanation.md) for architecture, limits, and integration notes.

## Tests

```bash
uv run pytest -q
```
