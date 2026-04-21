"""
Blackbox security agent runner.

Usage:
    python run_agent.py http://127.0.0.1:3000/#/
    uv run python run_agent.py http://127.0.0.1:3000/#/

The agent opens a real browser, navigates to the URL, and starts
blackbox security testing. Reasoning is printed to the terminal.

To swap the agent: replace agents/browser_use_agent.py with your
own implementation that exports  async def run(url: str) -> None
"""
from __future__ import annotations

import asyncio
import sys

# ── Swap this import to use a different agent ──────────────────────
from agents.browser_use_agent import run as agent_run
# ───────────────────────────────────────────────────────────────────


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python run_agent.py <target_url>")
        print("  e.g. python run_agent.py http://127.0.0.1:3000/#/")
        sys.exit(1)

    url = sys.argv[1].strip()
    print(f"\n[blackbox-agent] target → {url}")

    asyncio.run(agent_run(url))


if __name__ == "__main__":
    main()
