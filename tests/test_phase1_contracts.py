from __future__ import annotations

import json

from blackbox_service.engagement_models import EngagementRecord, ToolInvocation
from blackbox_service.settings import load_settings


def test_tool_invocation_roundtrip_and_engagement_defaults(tmp_path):
    ti = ToolInvocation(
        tool_name="nmap",
        target="http://example.com",
        args={"profile": "quick"},
        ok=True,
        cost_usd=0.02,
        artifacts=["nmap_out.xml"],
        error=None,
    )
    raw = ti.model_dump_json()
    reloaded = ToolInvocation.model_validate_json(raw)
    assert reloaded.tool_name == "nmap"
    assert reloaded.ok is True
    assert reloaded.artifacts == ["nmap_out.xml"]
    assert reloaded.error is None

    rec = EngagementRecord(engagement_id="eng-test", target_url="https://example.com")
    assert rec.tool_invocations == []


def test_hexstrike_settings_defaults(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    settings = load_settings(env_file=env_file)
    assert settings.hexstrike_enabled is False
    assert settings.hexstrike_url == "http://localhost:8888"
    assert settings.hexstrike_timeout_s == 300.0
    assert settings.tool_budget_hard_cap_usd == 5.0


def test_hexstrike_settings_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join([
            "BLACKBOX_HEXSTRIKE_ENABLED=true",
            "BLACKBOX_HEXSTRIKE_URL=http://hexstrike:8888",
            "BLACKBOX_HEXSTRIKE_TIMEOUT_S=120.0",
            "BLACKBOX_TOOL_BUDGET_HARD_CAP_USD=10.0",
        ]),
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file)
    assert settings.hexstrike_enabled is True
    assert settings.hexstrike_url == "http://hexstrike:8888"
    assert settings.hexstrike_timeout_s == 120.0
    assert settings.tool_budget_hard_cap_usd == 10.0
