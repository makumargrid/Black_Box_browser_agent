# Agentic Security Demo Reference

## Goal
Deliver a demo-safe, agentic blackbox testing system with:
- Orchestrator-managed engagement lifecycle
- Three capability agents (Discovery, Access/Test, Confirm/Evidence)
- Unified Browser Interaction Engine (BIE)
- Approval gate and executive reporting UI

## Runtime Shape
- Existing run/action engine remains active: `BlackboxService` + Playwright/InMemory runtime.
- New engagement layer wraps this engine and coordinates multi-agent execution.

## Core Components
- `blackbox_service/orchestrator.py`: engagement state machine and agent dispatch.
- `blackbox_service/bie/engine.py`: tiered execution abstraction.
- `blackbox_service/agents_v2/*`: three functional agents with adaptive loops.
- `blackbox_service/engagement_models.py`: engagement, findings, report, and approval models.
- `blackbox_service/api.py`: `/engagements/*` APIs + `/engagement-dashboard` for non-technical demos.

## Engagement Flow
1. `POST /engagements` to create an engagement.
2. `POST /engagements/{id}/start` to run Discovery -> Access/Test.
3. Optional approval pause occurs if findings exist and mode is `optional` or `mandatory`.
4. `POST /engagements/{id}/approval` to continue/reject.
5. Confirm/Evidence runs and report is produced.

## Demo Route
- Executive view: `/engagement-dashboard`
- Technical view: `/dashboard`

## Notes
- Anthropic mode remains fail-fast by default for Tier-4 AI navigation.
- Tier 3/5 adapters are intentionally marked unavailable in MVP runtime.
- Missing external scanner binaries do not crash orchestration.
