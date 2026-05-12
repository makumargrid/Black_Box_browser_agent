from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


@dataclass(slots=True)
class BIERequest:
    run_id: str
    goal: str
    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    preferred_tier: int | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BIEOutcome:
    ok: bool
    tier_used: int
    action_type: str
    result: Any = None
    error: str | None = None
    cost_usd: float = 0.0


class BrowserInteractionEngine:
    """Unified browser/HTTP abstraction with tier routing and lightweight middleware."""

    _TIER2_ACTIONS = {
        "open_tab",
        "switch_tab",
        "navigate",
        "eval_js",
        "inject_html",
        "read_console",
        "read_network",
        "snapshot",
        "click",
        "fill",
        "select_option",
        "wait_for_selector",
        "get_page_content",
    }

    _TIER1_ACTIONS = {"http_get", "http_post", "http_probe"}

    def __init__(
        self,
        action_executor: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        fail_fast_llm: bool = True,
        default_timeout: float = 15.0,
        anthropic_api_key: str = "",
        anthropic_model: str = "claude-opus-4-7",
        tier4_headless: bool = True,
    ) -> None:
        self._execute_action = action_executor
        self._fail_fast_llm = fail_fast_llm
        self._default_timeout = default_timeout
        self._anthropic_api_key = anthropic_api_key.strip()
        self._anthropic_model = anthropic_model.strip() or "claude-opus-4-7"
        self._tier4_headless = bool(tier4_headless)

    def request(self, req: BIERequest) -> BIEOutcome:
        tier = self._select_tier(req)
        self._apply_middleware_delay()
        try:
            if tier == 1:
                return self._handle_tier1(req)
            if tier == 2:
                return self._handle_tier2(req)
            if tier == 4:
                return self._handle_tier4(req)
            return BIEOutcome(
                ok=False,
                tier_used=tier,
                action_type=req.action_type,
                error=f"Tier {tier} is not available in MVP runtime",
            )
        except Exception as exc:
            return BIEOutcome(
                ok=False,
                tier_used=tier,
                action_type=req.action_type,
                error=str(exc),
            )

    def _select_tier(self, req: BIERequest) -> int:
        if req.preferred_tier in {1, 2, 4, 3, 5}:
            return int(req.preferred_tier)
        if req.action_type in self._TIER1_ACTIONS:
            return 1
        if req.action_type == "ai_navigate":
            return 4
        if req.action_type in self._TIER2_ACTIONS:
            return 2
        return 2

    def _apply_middleware_delay(self) -> None:
        delay_s = max(0.05, min(random.gauss(0.20, 0.12), 0.8))
        time.sleep(delay_s)

    def _normalized_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        }
        if extra:
            headers.update(extra)
        return headers

    def _handle_tier1(self, req: BIERequest) -> BIEOutcome:
        url = str(req.params.get("url", "")).strip()
        if not url:
            return BIEOutcome(
                ok=False,
                tier_used=1,
                action_type=req.action_type,
                error="url is required for tier1 action",
            )

        method = "GET"
        json_body = None
        if req.action_type == "http_post":
            method = "POST"
            json_body = req.params.get("json")

        with httpx.Client(timeout=self._default_timeout, follow_redirects=True) as client:
            response = client.request(
                method=method,
                url=url,
                headers=self._normalized_headers(req.params.get("headers")),
                json=json_body,
            )

        body_preview = response.text[:800]
        result = {
            "status_code": response.status_code,
            "url": str(response.url),
            "headers": dict(response.headers),
            "body_preview": body_preview,
        }
        ok = 200 <= response.status_code < 500
        return BIEOutcome(ok=ok, tier_used=1, action_type=req.action_type, result=result, cost_usd=0.0001)

    def _handle_tier2(self, req: BIERequest) -> BIEOutcome:
        out = self._execute_action(req.run_id, req.action_type, req.params)
        return BIEOutcome(
            ok=bool(out.get("ok", False)),
            tier_used=2,
            action_type=req.action_type,
            result=out.get("result"),
            cost_usd=0.001,
        )

    def _handle_tier4(self, req: BIERequest) -> BIEOutcome:
        target_url = str(req.params.get("url") or req.params.get("target_url") or "").strip()
        if not target_url:
            return BIEOutcome(
                ok=False,
                tier_used=4,
                action_type=req.action_type,
                error="target_url is required for ai_navigate",
            )

        if not self._anthropic_api_key:
            return BIEOutcome(
                ok=False,
                tier_used=4,
                action_type=req.action_type,
                error="Tier 4 requires ANTHROPIC_API_KEY loaded from .env",
            )

        instruction = str(
            req.params.get("instruction")
            or req.goal
            or f"Open {target_url} and reach the first authenticated dashboard page."
        ).strip()
        max_steps = int(req.params.get("max_steps", 12))
        task = (
            f"Target URL: {target_url}\n"
            f"Goal: {instruction}\n"
            "Return with the shortest successful path. Focus on auth/navigation flow only."
        )

        try:
            result = asyncio.run(self._run_browser_use_task(task=task, max_steps=max_steps))
            return BIEOutcome(
                ok=True,
                tier_used=4,
                action_type=req.action_type,
                result=result,
                cost_usd=0.02,
            )
        except Exception as exc:
            if self._fail_fast_llm:
                return BIEOutcome(
                    ok=False,
                    tier_used=4,
                    action_type=req.action_type,
                    error=f"Tier 4 fail-fast: {exc}",
                )
            return BIEOutcome(
                ok=True,
                tier_used=4,
                action_type=req.action_type,
                result={
                    "route_memory": [
                        {"action": "navigate", "url": target_url},
                        {"action": "get_page_content"},
                    ],
                    "compiled": False,
                    "note": f"Tier 4 fallback route due to error: {exc}",
                },
                cost_usd=0.005,
            )

    async def _run_browser_use_task(self, task: str, max_steps: int) -> dict[str, Any]:
        from browser_use import Agent, BrowserProfile
        from browser_use.llm.anthropic.chat import ChatAnthropic

        llm = ChatAnthropic(model=self._anthropic_model, api_key=self._anthropic_api_key, max_tokens=4096)
        profile = BrowserProfile(headless=self._tier4_headless, demo_mode=False)
        agent = Agent(
            task=task,
            llm=llm,
            browser_profile=profile,
            llm_timeout=300,
            max_actions_per_step=3,
        )
        history = await agent.run(max_steps=max_steps)

        urls = history.urls() if hasattr(history, "urls") else []
        actions = history.action_names() if hasattr(history, "action_names") else []
        errors = history.errors() if hasattr(history, "errors") else []
        final_result = history.final_result() if hasattr(history, "final_result") else ""
        return {
            "provider": "browser-use+anthropic",
            "model": self._anthropic_model,
            "steps_used": len(actions),
            "urls": urls,
            "actions": actions,
            "errors": errors,
            "route_memory": [{"action": "navigate", "url": u} for u in urls[:8]],
            "final_result": final_result,
        }
