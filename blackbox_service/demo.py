from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from blackbox_service.client import BlackboxClient
from blackbox_service.settings import load_settings


def _wait_for_health(url: str, timeout_seconds: float = 25.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _ensure_blackbox_service(base_url: str, base_env: dict[str, str]) -> subprocess.Popen[str] | None:
    health_url = f"{base_url.rstrip('/')}/health"
    if _wait_for_health(health_url, timeout_seconds=2.0):
        return None

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or 8080)

    env = os.environ.copy()
    env.update(base_env)
    env["BLACKBOX_HOST"] = host
    env["BLACKBOX_PORT"] = port

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "blackbox_service.main"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if _wait_for_health(health_url, timeout_seconds=30.0):
        return proc
    proc.terminate()
    proc.wait(timeout=5)
    raise RuntimeError("Blackbox service could not be started on the requested base URL.")


def build_dashboard_url(
    base_url: str,
    target_url: str,
    autorun: bool = True,
    autostart_agent: bool = True,
) -> str:
    query = urlencode(
        {
            "target": target_url,
            "autorun": "1" if autorun else "0",
            "autostart_agent": "1" if autostart_agent else "0",
        }
    )
    return f"{base_url.rstrip('/')}/dashboard?{query}"


def build_ops_console_url(base_url: str, target_url: str | None = None) -> str:
    if target_url:
        from urllib.parse import urlencode
        return f"{base_url.rstrip('/')}/ops-console?{urlencode({'target': target_url})}"
    return f"{base_url.rstrip('/')}/ops-console"


def build_engagement_dashboard_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/engagement-dashboard"


def run_demo_actions(client: Any, target_url: str) -> dict[str, Any]:
    run = client.create_run(
        targets=[target_url],
        options={"mode": "team-demo", "screenshot_policy": "on-change"},
    )
    run_id = run["run_id"]
    primary_tab_id = run["active_tab_id"]

    second_url = target_url.rstrip("/") + "/?bb_probe=1"
    actions: list[tuple[str, dict[str, Any]]] = [
        (
            "open_tab",
            {
                "url": second_url,
                "correlation_id": "demo-corr",
                "parent_tab_id": primary_tab_id,
            },
        ),
        ("switch_tab", {"tab_id": primary_tab_id}),
        ("navigate", {"tab_id": primary_tab_id, "url": target_url}),
        ("eval_js", {"tab_id": primary_tab_id, "script": "1 + 1"}),
        ("inject_html", {"tab_id": primary_tab_id, "html": "<div id='bb-demo'>blackbox demo</div>"}),
        ("read_console", {"tab_id": primary_tab_id}),
        ("read_network", {"tab_id": primary_tab_id}),
        ("snapshot", {"tab_id": primary_tab_id}),
    ]

    for action_type, params in actions:
        client.run_action(run_id=run_id, action_type=action_type, params=params)

    memory = client.list_memory(run_id)
    event_types = sorted({event.get("type", "") for event in memory.get("events", []) if event.get("type")})
    return {
        "run_id": run_id,
        "action_count": len(actions),
        "event_types": event_types,
        "memory_events": len(memory.get("events", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch live blackbox browser demo.")
    parser.add_argument("target_url", nargs="?", help="Target URL for the live run")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--no-autostart-agent", action="store_true", help="Create run automatically but do not start agent loop")
    parser.add_argument("--technical-dashboard", action="store_true", help="Open legacy technical dashboard instead of executive view")
    parser.add_argument("--ops-console", action="store_true", help="Open the cinematic SSE Operations Console (/ops-console)")
    args = parser.parse_args()

    settings = load_settings()
    base_url = os.getenv("DEMO_BLACKBOX_BASE_URL", settings.base_url)
    target_url = args.target_url or settings.default_target_url
    open_browser = (not args.no_browser) and settings.auto_open_browser
    autostart_agent = not args.no_autostart_agent

    startup_env = {
        "BLACKBOX_DB_PATH": settings.db_path,
        "BLACKBOX_USE_PLAYWRIGHT": "true" if settings.use_playwright else "false",
        "BLACKBOX_BROWSER_HEADLESS": "true" if settings.browser_headless else "false",
        "BLACKBOX_TARGET_URL": target_url,
        "BLACKBOX_AGENT_MODEL": settings.agent_model,
        "BLACKBOX_GEMINI_MODEL": settings.gemini_model,
        "BLACKBOX_AGENT_MAX_STEPS": str(settings.agent_max_steps),
        "BLACKBOX_AGENT_STEP_DELAY_MS": str(settings.agent_step_delay_ms),
    }
    if settings.anthropic_api_key:
        startup_env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.gemini_api_key:
        startup_env["GEMINI_API_KEY"] = settings.gemini_api_key

    _ = _ensure_blackbox_service(base_url=base_url, base_env=startup_env)
    dashboard_url = (
        build_dashboard_url(
            base_url=base_url,
            target_url=target_url,
            autorun=True,
            autostart_agent=autostart_agent,
        )
        if args.technical_dashboard
        else (
            build_ops_console_url(base_url=base_url, target_url=target_url)
            if args.ops_console
            else build_engagement_dashboard_url(base_url=base_url)
        )
    )

    print(f"Blackbox live dashboard: {dashboard_url}")
    if args.ops_console:
        print("  → Enter your target and click Create → Start to begin live streaming.")
    if open_browser:
        webbrowser.open(dashboard_url, new=1, autoraise=True)

    # Keep compatibility path for test harnesses that use deterministic action execution.
    if os.getenv("DEMO_RUN_BATCH_SMOKE", "false").lower() == "true":
        with httpx.Client(base_url=base_url, timeout=30.0) as http_client:
            client = BlackboxClient(http_client=http_client)
            summary = run_demo_actions(client=client, target_url=target_url)
        print(f"Batch smoke run completed: {summary['run_id']}")


if __name__ == "__main__":
    main()
