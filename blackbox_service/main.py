from __future__ import annotations

import os
import sys

from blackbox_service.agent import build_planner
from blackbox_service.api import create_app
from blackbox_service.settings import load_settings


def build_app():
    settings = load_settings()
    planner = build_planner(
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_model=settings.agent_model,
        gemini_api_key=settings.gemini_api_key,
        gemini_model=settings.gemini_model,
    )
    return create_app(
        db_path=settings.db_path,
        use_playwright=settings.use_playwright,
        browser_headless=settings.browser_headless,
        planner=planner,
        default_target_url=settings.default_target_url,
        default_agent_max_steps=settings.agent_max_steps,
        default_agent_step_delay_ms=settings.agent_step_delay_ms,
    )


def main() -> None:
    settings = load_settings()
    # Exec into uvicorn CLI so startup is isolated from any pre-existing event loop.
    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "blackbox_service.asgi:app",
            "--host",
            settings.host,
            "--port",
            str(settings.port),
        ],
    )


if __name__ == "__main__":
    main()
