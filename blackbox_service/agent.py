from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx


ALLOWED_ACTIONS = {
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
    "none",
}


@dataclass(slots=True)
class AgentDecision:
    thought: str
    hypothesis: str
    action_type: str
    params: dict[str, Any]
    done: bool = False


class Planner(Protocol):
    def next_decision(self, context: dict[str, Any]) -> AgentDecision:
        ...


class ScriptedPlanner:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self._idx = 0

    def next_decision(self, context: dict[str, Any]) -> AgentDecision:
        if self._idx >= len(self._script):
            return AgentDecision(
                thought="Script exhausted.",
                hypothesis="Stop this run.",
                action_type="none",
                params={},
                done=True,
            )
        item = self._script[self._idx]
        self._idx += 1
        return AgentDecision(
            thought=str(item.get("thought", "")),
            hypothesis=str(item.get("hypothesis", "")),
            action_type=str(item.get("action_type", "none")),
            params=dict(item.get("params", {})),
            done=bool(item.get("done", False)),
        )


class RuleBasedPlanner:
    def next_decision(self, context: dict[str, Any]) -> AgentDecision:
        step = int(context.get("step_index", 0))
        if step == 0:
            return AgentDecision(
                thought="Start with non-invasive recon signals.",
                hypothesis="Console and network should expose app stack clues.",
                action_type="read_console",
                params={},
            )
        if step == 1:
            return AgentDecision(
                thought="Expand recon into request/response telemetry.",
                hypothesis="Network traces may reveal hidden endpoints or APIs.",
                action_type="read_network",
                params={},
            )
        if step == 2:
            return AgentDecision(
                thought="Probe DOM state through JS execution.",
                hypothesis="Document metadata helps classify framework behavior.",
                action_type="eval_js",
                params={"script": "({title: document.title, url: location.href})"},
            )
        return AgentDecision(
            thought="Capture a persistent visual artifact.",
            hypothesis="A snapshot preserves context for later review.",
            action_type="snapshot",
            params={},
            done=True,
        )


class AnthropicPlanner:
    def __init__(self, api_key: str, model: str, timeout_seconds: float = 45.0) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout_seconds = timeout_seconds

    def next_decision(self, context: dict[str, Any]) -> AgentDecision:
        if not self._api_key:
            raise RuntimeError("Missing Anthropic API key in .env file")

        system_prompt = (
            "You are a blackbox security testing agent. You have ZERO prior knowledge of the target application. "
            "Discover everything by interacting with it. Your goal: find and confirm vulnerabilities.\n\n"
            "METHODOLOGY — work through each phase:\n"
            "1. RECON: Browse the app, map all pages and features. Note what requires login vs. what is public.\n"
            "2. AUTH TESTING: Find the login page. Try weak credentials (admin/admin, test/test, admin/password). "
            "Try SQL injection in the login form: ' OR 1=1--, ' OR '1'='1, admin'--\n"
            "3. XSS: Find every input field. Try: <script>alert(1)</script>, <img src=x onerror=alert(1)>, "
            "<svg onload=alert(1)>, <iframe src=\"javascript:alert(1)\">\n"
            "4. API DISCOVERY: Observe API calls the app makes as you browse. Test discovered endpoints "
            "without auth, with different HTTP methods, with different IDs.\n"
            "5. ACCESS CONTROL: Try any admin/restricted routes discovered during recon.\n"
            "6. IDOR: When you see numeric IDs in URLs or responses, try changing them to access other records.\n"
            "7. SENSITIVE DATA: Check /robots.txt, /ftp, /.env, error pages for stack traces, "
            "API responses for passwords/tokens/PII.\n\n"
            "Return ONLY valid JSON with exactly these keys:\n"
            '{ "thought": "...", "hypothesis": "...", "action_type": "...", "params": {...}, "done": false }\n\n'
            f"Allowed action_type values: {sorted(ALLOWED_ACTIONS)}\n\n"
            "Params by action:\n"
            "- get_page_content/snapshot/read_console/read_network: {\"tab_id\": \"...\"}\n"
            "- click: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\"}\n"
            "- fill: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\", \"value\": \"text_to_type\"}\n"
            "- select_option: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\", \"value\": \"option_value\"}\n"
            "- wait_for_selector: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\", \"timeout_ms\": 3000}\n"
            "- navigate/open_tab: {\"tab_id\": \"...\", \"url\": \"full_url\"}\n"
            "- eval_js: {\"tab_id\": \"...\", \"script\": \"JS_expression\"}\n"
            "- none (finished): {}, set done=true\n\n"
            "Be systematic. Discover first, exploit second. No hardcoded assumptions about the target."
        )
        user_prompt = json.dumps(context, ensure_ascii=True)
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text
                raise RuntimeError(
                    f"Anthropic API {exc.response.status_code}: {body}"
                ) from exc
            data = response.json()

        content = data.get("content", [])
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_chunks.append(str(item.get("text", "")))
        raw_text = "\n".join(text_chunks).strip()
        parsed = _parse_json_object(raw_text)
        return _coerce_decision(parsed)


class GeminiPlanner:
    def __init__(self, api_key: str, model: str, timeout_seconds: float = 45.0) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip() or "gemini-2.5-flash"
        self._timeout_seconds = timeout_seconds

    def next_decision(self, context: dict[str, Any]) -> AgentDecision:
        if not self._api_key:
            raise RuntimeError("Missing Gemini API key in .env file")

        system_prompt = (
            "You are a blackbox security testing agent. You have ZERO prior knowledge of the target application. "
            "Discover everything by interacting with it. Your goal: find and confirm vulnerabilities.\n\n"
            "METHODOLOGY — work through each phase:\n"
            "1. RECON: Browse the app, map all pages and features. Note what requires login vs. what is public.\n"
            "2. AUTH TESTING: Find the login page. Try weak credentials (admin/admin, test/test, admin/password). "
            "Try SQL injection in the login form: ' OR 1=1--, ' OR '1'='1, admin'--\n"
            "3. XSS: Find every input field. Try: <script>alert(1)</script>, <img src=x onerror=alert(1)>, "
            "<svg onload=alert(1)>, <iframe src=\"javascript:alert(1)\">\n"
            "4. API DISCOVERY: Observe API calls the app makes as you browse. Test discovered endpoints "
            "without auth, with different HTTP methods, with different IDs.\n"
            "5. ACCESS CONTROL: Try any admin/restricted routes discovered during recon.\n"
            "6. IDOR: When you see numeric IDs in URLs or responses, try changing them to access other records.\n"
            "7. SENSITIVE DATA: Check /robots.txt, /ftp, /.env, error pages for stack traces, "
            "API responses for passwords/tokens/PII.\n\n"
            "Return ONLY valid JSON with exactly these keys:\n"
            '{ "thought": "...", "hypothesis": "...", "action_type": "...", "params": {...}, "done": false }\n\n'
            f"Allowed action_type values: {sorted(ALLOWED_ACTIONS)}\n\n"
            "Params by action:\n"
            "- get_page_content/snapshot/read_console/read_network: {\"tab_id\": \"...\"}\n"
            "- click: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\"}\n"
            "- fill: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\", \"value\": \"text_to_type\"}\n"
            "- select_option: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\", \"value\": \"option_value\"}\n"
            "- wait_for_selector: {\"tab_id\": \"...\", \"selector\": \"CSS_selector\", \"timeout_ms\": 3000}\n"
            "- navigate/open_tab: {\"tab_id\": \"...\", \"url\": \"full_url\"}\n"
            "- eval_js: {\"tab_id\": \"...\", \"script\": \"JS_expression\"}\n"
            "- none (finished): {}, set done=true\n\n"
            "Be systematic. Discover first, exploit second. No hardcoded assumptions about the target."
        )

        request_payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(context, ensure_ascii=True)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
        }
        encoded_model = quote(self._model, safe="")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent"
            f"?key={self._api_key}"
        )
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(url, json=request_payload)
            response.raise_for_status()
            data = response.json()

        text_parts: list[str] = []
        for candidate in data.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if isinstance(part, dict) and "text" in part:
                    text_parts.append(str(part.get("text", "")))
        raw_text = "\n".join(text_parts).strip()
        parsed = _parse_json_object(raw_text)
        return _coerce_decision(parsed)


class FailoverPlanner:
    def __init__(self, primary: Planner, fallback: Planner) -> None:
        self._primary = primary
        self._fallback = fallback
        self._using_fallback = False

    def next_decision(self, context: dict[str, Any]) -> AgentDecision:
        if self._using_fallback:
            return self._fallback.next_decision(context)
        try:
            return self._primary.next_decision(context)
        except Exception:
            self._using_fallback = True
            return self._fallback.next_decision(context)


def build_planner(
    anthropic_api_key: str,
    anthropic_model: str,
    gemini_api_key: str = "",
    gemini_model: str = "gemini-2.5-flash",
) -> Planner:
    anthropic_key = anthropic_api_key.strip()
    gemini_key = gemini_api_key.strip()

    if anthropic_key and gemini_key:
        return FailoverPlanner(
            primary=AnthropicPlanner(api_key=anthropic_key, model=anthropic_model),
            fallback=GeminiPlanner(api_key=gemini_key, model=gemini_model),
        )
    if anthropic_key:
        return AnthropicPlanner(api_key=anthropic_key, model=anthropic_model)
    if gemini_key:
        return GeminiPlanner(api_key=gemini_key, model=gemini_model)
    return RuleBasedPlanner()


def _parse_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        return json.loads(raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Planner response did not include JSON object")
    return json.loads(raw[start : end + 1])


def _coerce_decision(raw: dict[str, Any]) -> AgentDecision:
    action_type = str(raw.get("action_type", "none"))
    if action_type not in ALLOWED_ACTIONS:
        action_type = "none"
    params = raw.get("params", {})
    if not isinstance(params, dict):
        params = {}
    return AgentDecision(
        thought=str(raw.get("thought", "")),
        hypothesis=str(raw.get("hypothesis", "")),
        action_type=action_type,
        params=params,
        done=bool(raw.get("done", False)),
    )
