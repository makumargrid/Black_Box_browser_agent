from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from .base import AgentBase, AgentContext, AgentStep


_LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_PATH_RE = re.compile(r"(/api/[A-Za-z0-9_\-/]+)")

_SYSTEM_PROMPT = """\
You are a blackbox security reconnaissance agent. Your phase: ATTACK SURFACE DISCOVERY.

Goal: Map everything about the target web app — pages, API endpoints, auth requirements, hidden paths, tech stack.

Strategy (follow this order, but adapt based on what you discover):
1. First call get_page_content to understand the app type (SPA vs server-rendered)
2. Probe well-known sensitive paths: /robots.txt, /ftp, /.env, /backup, /admin, /swagger, /graphql, /api
3. Use http_get on the base URL and any paths you discover
4. Follow links and API paths found in page content / responses
5. Note which paths return 401/403 (auth required) vs 200 (public)
6. When you've mapped the surface thoroughly, set done=true

Available actions:
- http_get: {"url": "full_url"} — fast HTTP probe, returns status + body preview
- get_page_content: {} — read current browser page (use at start for SPA detection)
- navigate: {"url": "full_url"} — navigate browser to URL

Return ONLY valid JSON (no explanation, no markdown):
{"thought": "...", "hypothesis": "...", "action_type": "...", "params": {...}, "done": false}

Set done=true only when attack surface is thoroughly mapped. Be systematic.\
"""


class DiscoveryAgent(AgentBase):
    name = "discovery"

    def initialize_state(self, ctx: AgentContext) -> dict[str, object]:
        return {
            "endpoints": [],
            "seen_urls": [],
            "hosts": [urlparse(ctx.target_url).netloc],
            "total_cost_usd": 0.0,
        }

    def plan_next(self, ctx: AgentContext, local_state: dict[str, object], observations: list[dict[str, object]]) -> AgentStep:
        decision = self._call_llm(ctx, _SYSTEM_PROMPT, {
            "target_url": ctx.target_url,
            "step": len(observations),
            "max_steps": ctx.max_steps,
            "endpoints_found": len(local_state.get("endpoints", [])),
            "recent_observations": [
                {
                    "action_type": o.get("action_type"),
                    "ok": o.get("ok"),
                    "result_preview": str(o.get("result", ""))[:300],
                }
                for o in observations[-6:]
            ],
            "allowed_actions": ["http_get", "get_page_content", "navigate"],
        })
        return AgentStep(
            done=bool(decision.get("done", False)),
            goal=str(decision.get("thought", "")),
            action_type=str(decision.get("action_type", "none")),
            params=dict(decision.get("params") or {}),
            note=str(decision.get("hypothesis", "")),
        )

    def _after_observation(self, local_state: dict[str, object], obs: dict[str, object]) -> None:
        super()._after_observation(local_state, obs)
        action_type = str(obs.get("action_type", ""))
        result = obs.get("result") or {}

        if action_type == "http_get" and isinstance(result, dict):
            target_url = str(result.get("url", ""))
            status_code = int(result.get("status_code", 0))
            body_preview = str(result.get("body_preview", ""))
            headers = result.get("headers") or {}

            local_state["endpoints"].append(
                {
                    "url": target_url,
                    "method": "GET",
                    "status_code": status_code,
                    "source": "discovery-http",
                    "auth_required": status_code in {401, 403},
                }
            )

            for m in _LINK_RE.finditer(body_preview):
                href = m.group(1)
                if href.startswith("javascript:"):
                    continue
                if href.startswith("http://") or href.startswith("https://"):
                    candidate = href
                else:
                    candidate = urljoin(target_url, href)
                if candidate not in local_state["seen"] and candidate not in local_state["queue"]:
                    local_state["queue"].append(candidate)

            for m in _PATH_RE.finditer(body_preview):
                local_state["endpoints"].append(
                    {
                        "url": m.group(1),
                        "method": "GET",
                        "status_code": status_code,
                        "source": "js-path-hint",
                        "auth_required": False,
                    }
                )

            server = str(headers.get("server", "")).strip()
            if server:
                local_state.setdefault("tech_stack", set()).add(server)

    def summarize(self, ctx: AgentContext, local_state: dict[str, object], observations: list[dict[str, object]]) -> dict[str, object]:
        dedup = {}
        for item in local_state.get("endpoints", []):
            if not isinstance(item, dict):
                continue
            key = f"{item.get('method','GET')}|{item.get('url','')}"
            dedup[key] = item

        return {
            "hosts": sorted(local_state.get("hosts", set())),
            "endpoints": list(dedup.values()),
            "tech_stack": sorted(local_state.get("tech_stack", set())),
            "observation_count": len(observations),
            "cost_usd": float(local_state.get("total_cost_usd", 0.0)),
            "observations": observations,
        }
