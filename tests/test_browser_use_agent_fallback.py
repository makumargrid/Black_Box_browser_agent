from __future__ import annotations

import asyncio
import importlib
import sys
import types


def _load_module_with_stubs():
    # Ensure a clean import for each test.
    sys.modules.pop("agents.browser_use_agent", None)

    browser_use_mod = types.ModuleType("browser_use")
    browser_use_mod.Agent = object
    browser_use_mod.BrowserProfile = object

    llm_pkg = types.ModuleType("browser_use.llm")
    llm_anthropic_pkg = types.ModuleType("browser_use.llm.anthropic")
    llm_google_pkg = types.ModuleType("browser_use.llm.google")
    llm_ex_mod = types.ModuleType("browser_use.llm.exceptions")
    llm_anthropic_chat_mod = types.ModuleType("browser_use.llm.anthropic.chat")
    llm_google_chat_mod = types.ModuleType("browser_use.llm.google.chat")

    class _ModelProviderError(Exception):
        def __init__(self, message: str, status_code: int = 502, model: str | None = None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code
            self.model = model

    class _ModelRateLimitError(_ModelProviderError):
        pass

    class _ChatAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _ChatGoogle:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    llm_ex_mod.ModelProviderError = _ModelProviderError
    llm_ex_mod.ModelRateLimitError = _ModelRateLimitError
    llm_anthropic_chat_mod.ChatAnthropic = _ChatAnthropic
    llm_google_chat_mod.ChatGoogle = _ChatGoogle

    display_mod = types.ModuleType("agents.display")
    display_mod.attach = lambda agent: None

    sys.modules["browser_use"] = browser_use_mod
    sys.modules["browser_use.llm"] = llm_pkg
    sys.modules["browser_use.llm.anthropic"] = llm_anthropic_pkg
    sys.modules["browser_use.llm.google"] = llm_google_pkg
    sys.modules["browser_use.llm.exceptions"] = llm_ex_mod
    sys.modules["browser_use.llm.anthropic.chat"] = llm_anthropic_chat_mod
    sys.modules["browser_use.llm.google.chat"] = llm_google_chat_mod
    sys.modules["agents.display"] = display_mod

    return importlib.import_module("agents.browser_use_agent"), _ModelProviderError


def test_convert_anthropic_400_usage_limit_to_fallback_eligible():
    bua, model_provider_error = _load_module_with_stubs()

    error = model_provider_error(
        message="You have reached your specified workspace API usage limits.",
        status_code=400,
        model="claude-opus-4-7",
    )
    assert bua._should_convert_anthropic_error_to_rate_limit(error) is True


def test_do_not_convert_unrelated_anthropic_400_error():
    bua, model_provider_error = _load_module_with_stubs()

    error = model_provider_error(
        message="invalid tool schema",
        status_code=400,
        model="claude-opus-4-7",
    )
    assert bua._should_convert_anthropic_error_to_rate_limit(error) is False


def test_build_llms_uses_anthropic_primary_and_gemini_fallback(monkeypatch):
    bua, _ = _load_module_with_stubs()

    class _Anthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Gemini:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bua, "ChatAnthropicWithFallback", _Anthropic)
    monkeypatch.setattr(bua, "ChatGoogle", _Gemini)

    primary, fallback, active_model = bua._build_llms(
        anthropic_api_key="ant-key",
        anthropic_model="claude-opus-4-7",
        gemini_api_key="gem-key",
        gemini_model="gemini-2.5-flash",
    )

    assert isinstance(primary, _Anthropic)
    assert isinstance(fallback, _Gemini)
    assert active_model == "claude-opus-4-7"
    assert fallback.kwargs["vertexai"] is False


def test_build_llms_uses_gemini_only_when_anthropic_missing(monkeypatch):
    bua, _ = _load_module_with_stubs()

    class _Gemini:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bua, "ChatGoogle", _Gemini)

    primary, fallback, active_model = bua._build_llms(
        anthropic_api_key="",
        anthropic_model="claude-opus-4-7",
        gemini_api_key="gem-key",
        gemini_model="gemini-2.5-flash",
    )

    assert isinstance(primary, _Gemini)
    assert fallback is None
    assert active_model == "gemini-2.5-flash"
    assert primary.kwargs["vertexai"] is False


def test_run_uses_shared_task_prompt_template(monkeypatch):
    bua, _ = _load_module_with_stubs()

    class _History:
        def final_result(self):
            return None

    captured: dict[str, object] = {}

    class _Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, max_steps):
            captured["max_steps"] = max_steps
            return _History()

    class _Profile:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bua, "Agent", _Agent)
    monkeypatch.setattr(bua, "BrowserProfile", _Profile)
    monkeypatch.setattr(bua, "attach", lambda agent: None)
    monkeypatch.setattr(
        bua,
        "_env",
        {
            "ANTHROPIC_API_KEY": "ant-key",
            "GEMINI_API_KEY": "gem-key",
            "BLACKBOX_AGENT_MODEL": "claude-opus-4-7",
            "BLACKBOX_GEMINI_MODEL": "gemini-2.5-flash",
            "BLACKBOX_AGENT_MAX_STEPS": "7",
        },
    )

    asyncio.run(bua.run("http://127.0.0.1:3000/#/"))

    assert captured["task"] == bua._TASK.format(url="http://127.0.0.1:3000/#/")
    assert captured["max_steps"] == 7
