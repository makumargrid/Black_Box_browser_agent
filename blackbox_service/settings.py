from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

# DEFAULT_AGENT_MODEL = "claude-3-7-sonnet-latest"
DEFAULT_AGENT_MODEL = "claude-sonnet-4-6"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _to_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(raw: Any, default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _to_float(raw: Any, default: float) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


@dataclass(slots=True)
class BlackboxSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: str = "blackbox_events.db"
    use_playwright: bool = True
    browser_headless: bool = False
    default_target_url: str = "http://127.0.0.1:3000/#/"
    agent_model: str = DEFAULT_AGENT_MODEL
    gemini_model: str = DEFAULT_GEMINI_MODEL
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    agent_max_steps: int = 20
    agent_step_delay_ms: int = 1000
    auto_open_browser: bool = True
    strict_playwright_runtime: bool = False
    hexstrike_enabled: bool = True   # on by default; graceful degradation when server is absent
    hexstrike_url: str = "http://localhost:8888"
    hexstrike_timeout_s: float = 300.0
    tool_budget_hard_cap_usd: float = 5.0

    @property
    def base_url(self) -> str:
        host = self.host
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"


def load_settings(env_file: str | Path = ".env") -> BlackboxSettings:
    file_values = {
        key: value
        for key, value in dotenv_values(env_file).items()
        if value is not None
    }

    def pick(name: str, default: Any) -> Any:
        env_value = os.getenv(name)
        if env_value is not None:
            return env_value
        if name in file_values:
            return file_values[name]
        return default

    return BlackboxSettings(
        host=str(pick("BLACKBOX_HOST", "127.0.0.1")),
        port=_to_int(pick("BLACKBOX_PORT", "8080"), 8080),
        db_path=str(pick("BLACKBOX_DB_PATH", "blackbox_events.db")),
        use_playwright=_to_bool(pick("BLACKBOX_USE_PLAYWRIGHT", "true"), True),
        browser_headless=_to_bool(pick("BLACKBOX_BROWSER_HEADLESS", "false"), False),
        default_target_url=str(pick("BLACKBOX_TARGET_URL", "http://127.0.0.1:3000/#/")),
        agent_model=str(pick("BLACKBOX_AGENT_MODEL", DEFAULT_AGENT_MODEL)),
        gemini_model=str(pick("BLACKBOX_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)),
        # Security requirement: key is loaded only from the .env file, never from shell env.
        anthropic_api_key=str(file_values.get("ANTHROPIC_API_KEY", "")),
        # Security requirement: key is loaded only from the .env file, never from shell env.
        gemini_api_key=str(file_values.get("GEMINI_API_KEY", file_values.get("GOOGLE_API_KEY", ""))),
        agent_max_steps=_to_int(pick("BLACKBOX_AGENT_MAX_STEPS", "20"), 20),
        agent_step_delay_ms=_to_int(pick("BLACKBOX_AGENT_STEP_DELAY_MS", "1000"), 1000),
        auto_open_browser=_to_bool(pick("BLACKBOX_AUTO_OPEN_BROWSER", "true"), True),
        strict_playwright_runtime=_to_bool(pick("BLACKBOX_STRICT_PLAYWRIGHT_RUNTIME", "false"), False),
        hexstrike_enabled=_to_bool(pick("BLACKBOX_HEXSTRIKE_ENABLED", "true"), True),
        hexstrike_url=str(pick("BLACKBOX_HEXSTRIKE_URL", "http://localhost:8888")),
        hexstrike_timeout_s=_to_float(pick("BLACKBOX_HEXSTRIKE_TIMEOUT_S", "300.0"), 300.0),
        tool_budget_hard_cap_usd=_to_float(pick("BLACKBOX_TOOL_BUDGET_HARD_CAP_USD", "5.0"), 5.0),
    )
