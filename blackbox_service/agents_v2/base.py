from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import httpx

from blackbox_service.bie import BIERequest, BrowserInteractionEngine

if TYPE_CHECKING:
    from blackbox_service.toolchannel.security_gate import SecurityToolGate


@dataclass(slots=True)
class AgentContext:
    engagement_id: str
    run_id: str
    target_url: str
    max_steps: int = 12
    step_delay_ms: int = 200
    state: dict[str, Any] = field(default_factory=dict)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"


@dataclass(slots=True)
class AgentStep:
    done: bool
    goal: str = ""
    action_type: str = "none"
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""


class AgentBase:
    name = "base"

    # Subclasses declare the action_types they handle via the ToolChannel.
    # The base set is empty — BIE handles everything by default.
    _TOOL_ACTION_NAMES: frozenset[str] = frozenset()

    def __init__(
        self,
        bie: BrowserInteractionEngine,
        tool_gate: SecurityToolGate | None = None,
        step_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._bie = bie
        self._tool_gate = tool_gate
        self._step_sink = step_sink

    def initialize_state(self, ctx: AgentContext) -> dict[str, Any]:
        return {}

    def plan_next(self, ctx: AgentContext, local_state: dict[str, Any], observations: list[dict[str, Any]]) -> AgentStep:
        raise NotImplementedError

    def summarize(self, ctx: AgentContext, local_state: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
        return {"observations": observations, "state": local_state}

    def _effective_tool_names(self, ctx: AgentContext) -> frozenset[str]:
        """Union of hardcoded class tool names and any tools dynamically discovered from HexStrike."""
        dynamic = frozenset(
            t["name"] for t in ctx.state.get("available_tools", [])
            if isinstance(t, dict) and t.get("name")
        )
        return self._TOOL_ACTION_NAMES | dynamic

    def run(self, ctx: AgentContext) -> dict[str, Any]:
        local_state = self.initialize_state(ctx)
        observations: list[dict[str, Any]] = []
        effective_tool_names = self._effective_tool_names(ctx)

        for _ in range(ctx.max_steps):
            step = self.plan_next(ctx, local_state, observations)
            if step.done:
                break

            if step.action_type in effective_tool_names:
                # Route through the ToolChannel instead of the BIE.
                tool_result = self._invoke_tool(step.action_type, step.params)
                observations.append(
                    {
                        "goal": step.goal,
                        "action_type": step.action_type,
                        "ok": tool_result.get("ok", False),
                        "tier": "tool",
                        "result": tool_result.get("raw"),
                        "stdout": tool_result.get("stdout", ""),
                        "artifacts": tool_result.get("artifacts", []),
                        "error": tool_result.get("error"),
                        "cost_usd": 0.0,  # cost tracked by SecurityToolGate
                        "note": step.note,
                        "tool_result": tool_result,
                    }
                )
            else:
                out = self._bie.request(
                    BIERequest(
                        run_id=ctx.run_id,
                        goal=step.goal,
                        action_type=step.action_type,
                        params=step.params,
                    )
                )
                observations.append(
                    {
                        "goal": step.goal,
                        "action_type": step.action_type,
                        "ok": out.ok,
                        "tier": out.tier_used,
                        "result": out.result,
                        "error": out.error,
                        "cost_usd": out.cost_usd,
                        "note": step.note,
                    }
                )

            self._after_observation(local_state, observations[-1])

            if ctx.step_delay_ms > 0:
                time.sleep(ctx.step_delay_ms / 1000.0)

            # Emit per-step reasoning event for live SSE visibility in Ops Console.
            if self._step_sink is not None:
                obs = observations[-1]
                self._step_sink("agent.step", {
                    "agent": self.name,
                    "step": len(observations),
                    "thought": step.goal,
                    "action": step.action_type,
                    "ok": obs.get("ok"),
                    "error": obs.get("error"),
                    "result_preview": str(obs.get("result") or obs.get("stdout", ""))[:200],
                })

        return self.summarize(ctx, local_state, observations)

    def _invoke_tool(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """Invoke *tool* through the SecurityToolGate.

        Returns a clean negative dict when no gate is configured, so callers
        can treat the disabled-HexStrike path identically to the enabled path.
        """
        if self._tool_gate is None:
            return {
                "ok": False,
                "raw": None,
                "stdout": "",
                "artifacts": [],
                "error": "no_tool_gate",
            }
        return self._tool_gate.invoke(tool, params)

    def _after_observation(self, local_state: dict[str, Any], obs: dict[str, Any]) -> None:
        local_state["total_cost_usd"] = float(local_state.get("total_cost_usd", 0.0)) + float(obs.get("cost_usd", 0.0))

    def _call_llm(self, ctx: AgentContext, system_prompt: str, user_context: dict[str, Any]) -> dict[str, Any]:
        """Call Anthropic Claude and return a parsed decision dict.

        Expected response keys: thought, hypothesis, action_type, params, done.
        Returns done=True on any error so the agent terminates gracefully.
        """
        if not ctx.anthropic_api_key:
            return {
                "action_type": "none", "done": True,
                "thought": "No ANTHROPIC_API_KEY in .env — cannot call LLM.",
                "hypothesis": "", "params": {},
            }

        payload = {
            "model": ctx.anthropic_model,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": json.dumps(user_context, ensure_ascii=True)}],
        }
        headers = {
            "x-api-key": ctx.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text
                    raise RuntimeError(f"Anthropic API {exc.response.status_code}: {body}") from exc
                data = resp.json()
        except Exception as exc:
            return {
                "action_type": "none", "done": True,
                "thought": f"LLM call failed: {exc}", "hypothesis": "", "params": {},
            }

        text = "\n".join(
            item["text"]
            for item in data.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"action_type": "none", "done": True, "thought": text, "hypothesis": "", "params": {}}
        try:
            parsed = json.loads(m.group())
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed
        except (json.JSONDecodeError, ValueError):
            return {"action_type": "none", "done": True, "thought": text, "hypothesis": "", "params": {}}
