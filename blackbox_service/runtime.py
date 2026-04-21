from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blackbox_service.models import TabState

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright


@dataclass
class _RunState:
    tabs: dict[str, TabState] = field(default_factory=dict)
    active_tab_id: str | None = None
    tab_html: dict[str, str] = field(default_factory=dict)
    console_logs: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    network_events: dict[str, list[dict[str, str | int]]] = field(default_factory=dict)


@dataclass
class _PlaywrightRunState:
    context: Any
    pages: dict[str, Any] = field(default_factory=dict)
    active_tab_id: str | None = None
    console_logs: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    network_events: dict[str, list[dict[str, str | int]]] = field(default_factory=dict)


class InMemoryRuntime:
    """Deterministic runtime used for local tests and offline capability mode."""

    def __init__(self, artifacts_dir: str | Path = "artifacts") -> None:
        self._runs: dict[str, _RunState] = {}
        self._artifacts_dir = Path(artifacts_dir)

    def start_run(self, run_id: str, targets: list[str]) -> str | None:
        state = self._runs.setdefault(run_id, _RunState())
        if targets:
            first = self.open_tab(run_id=run_id, url=targets[0], correlation_id="seed-target")
            self.switch_tab(run_id, first.tab_id)
            return first.tab_id
        return None

    def open_tab(
        self,
        run_id: str,
        url: str,
        correlation_id: str | None = None,
        parent_tab_id: str | None = None,
    ) -> TabState:
        if run_id not in self._runs:
            self._runs[run_id] = _RunState()
        state = self._runs[run_id]
        tab_id = f"tab-{uuid.uuid4().hex[:8]}"
        tab = TabState(
            run_id=run_id,
            tab_id=tab_id,
            url=url,
            title=url,
            parent_tab_id=parent_tab_id,
            correlation_id=correlation_id,
            is_active=False,
        )
        state.tabs[tab_id] = tab
        state.console_logs[tab_id] = []
        state.network_events[tab_id] = []
        return tab

    def switch_tab(self, run_id: str, tab_id: str) -> None:
        state = self._runs[run_id]
        if tab_id not in state.tabs:
            raise KeyError(f"Unknown tab_id {tab_id}")
        for t_id, tab in list(state.tabs.items()):
            state.tabs[t_id] = tab.model_copy(update={"is_active": t_id == tab_id})
        state.active_tab_id = tab_id

    def navigate_tab(self, run_id: str, tab_id: str, url: str) -> TabState:
        state = self._runs[run_id]
        tab = state.tabs[tab_id].model_copy(update={"url": url, "title": url})
        state.tabs[tab_id] = tab
        return tab

    def eval_js(self, run_id: str, tab_id: str, script: str):
        _ = self._runs[run_id].tabs[tab_id]
        return _safe_eval_expression(script)

    def inject_html(self, run_id: str, tab_id: str, html: str) -> None:
        _ = self._runs[run_id].tabs[tab_id]
        self._runs[run_id].tab_html[tab_id] = html

    def get_console_logs(self, run_id: str, tab_id: str) -> list[dict[str, str]]:
        state = self._runs[run_id]
        return list(state.console_logs.get(tab_id, []))

    def get_network_events(self, run_id: str, tab_id: str) -> list[dict[str, str | int]]:
        state = self._runs[run_id]
        return list(state.network_events.get(tab_id, []))

    def list_tabs(self, run_id: str) -> list[TabState]:
        state = self._runs[run_id]
        return list(state.tabs.values())

    def get_active_tab(self, run_id: str) -> str | None:
        return self._runs[run_id].active_tab_id

    def click(self, run_id: str, tab_id: str, selector: str) -> dict:
        _ = self._runs[run_id].tabs[tab_id]
        return {"ok": True, "selector": selector}

    def fill(self, run_id: str, tab_id: str, selector: str, value: str) -> dict:
        _ = self._runs[run_id].tabs[tab_id]
        return {"ok": True, "selector": selector, "value": value}

    def select_option(self, run_id: str, tab_id: str, selector: str, value: str) -> dict:
        _ = self._runs[run_id].tabs[tab_id]
        return {"ok": True, "selector": selector, "value": value}

    def wait_for_selector(self, run_id: str, tab_id: str, selector: str, timeout_ms: int = 5000) -> dict:
        _ = self._runs[run_id].tabs[tab_id]
        return {"ok": True, "found": True, "selector": selector}

    def get_page_content(self, run_id: str, tab_id: str, max_chars: int = 4000) -> dict:
        tab = self._runs[run_id].tabs[tab_id]
        return {"url": tab.url, "title": tab.title, "text": "(offline)", "inputs": [], "links": []}

    def capture_screenshot(self, run_id: str, tab_id: str, artifact_name: str) -> str:
        _ = self._runs[run_id].tabs[tab_id]
        path = self._artifacts_dir / run_id / artifact_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("offline-screenshot-placeholder", encoding="utf-8")
        return str(path)


class PlaywrightRuntime:
    """Playwright-backed runtime for real browser tab interactions."""

    def __init__(self, headless: bool = True, artifacts_dir: str | Path = "artifacts") -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._runs: dict[str, _PlaywrightRunState] = {}
        self._artifacts_dir = Path(artifacts_dir)

    def start_run(self, run_id: str, targets: list[str]) -> str | None:
        context = self._browser.new_context()
        state = _PlaywrightRunState(context=context)
        self._runs[run_id] = state
        if targets:
            first = self.open_tab(run_id=run_id, url=targets[0], correlation_id="seed-target")
            self.switch_tab(run_id, first.tab_id)
            return first.tab_id
        return None

    def stop_run(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        state.context.close()
        self._runs.pop(run_id, None)

    def open_tab(
        self,
        run_id: str,
        url: str,
        correlation_id: str | None = None,
        parent_tab_id: str | None = None,
    ) -> TabState:
        state = self._runs[run_id]
        page = state.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        tab_id = f"tab-{uuid.uuid4().hex[:8]}"
        state.pages[tab_id] = page
        state.console_logs[tab_id] = []
        state.network_events[tab_id] = []

        def _on_console(msg):
            state.console_logs[tab_id].append(
                {
                    "type": msg.type,
                    "text": msg.text,
                }
            )

        def _on_request(req):
            state.network_events[tab_id].append(
                {
                    "kind": "request",
                    "method": req.method,
                    "url": req.url,
                }
            )

        def _on_response(res):
            state.network_events[tab_id].append(
                {
                    "kind": "response",
                    "status": int(res.status),
                    "url": res.url,
                }
            )

        page.on("console", _on_console)
        page.on("request", _on_request)
        page.on("response", _on_response)
        return TabState(
            run_id=run_id,
            tab_id=tab_id,
            url=page.url,
            title=page.title(),
            parent_tab_id=parent_tab_id,
            correlation_id=correlation_id,
            is_active=False,
        )

    def switch_tab(self, run_id: str, tab_id: str) -> None:
        state = self._runs[run_id]
        if tab_id not in state.pages:
            raise KeyError(f"Unknown tab_id {tab_id}")
        state.active_tab_id = tab_id

    def navigate_tab(self, run_id: str, tab_id: str, url: str) -> TabState:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        page.goto(url, wait_until="domcontentloaded")
        return TabState(
            run_id=run_id,
            tab_id=tab_id,
            url=page.url,
            title=page.title(),
            is_active=(state.active_tab_id == tab_id),
        )

    def eval_js(self, run_id: str, tab_id: str, script: str):
        state = self._runs[run_id]
        page = state.pages[tab_id]
        return page.evaluate(script)

    def inject_html(self, run_id: str, tab_id: str, html: str) -> None:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        page.evaluate(
            """(payload) => {
                document.body.insertAdjacentHTML("beforeend", payload);
            }""",
            html,
        )

    def get_console_logs(self, run_id: str, tab_id: str) -> list[dict[str, str]]:
        state = self._runs[run_id]
        return list(state.console_logs.get(tab_id, []))

    def get_network_events(self, run_id: str, tab_id: str) -> list[dict[str, str | int]]:
        state = self._runs[run_id]
        return list(state.network_events.get(tab_id, []))

    def list_tabs(self, run_id: str) -> list[TabState]:
        state = self._runs[run_id]
        tabs: list[TabState] = []
        for tab_id, page in state.pages.items():
            tabs.append(
                TabState(
                    run_id=run_id,
                    tab_id=tab_id,
                    url=page.url,
                    title=page.title(),
                    is_active=(state.active_tab_id == tab_id),
                )
            )
        return tabs

    def get_active_tab(self, run_id: str) -> str | None:
        return self._runs[run_id].active_tab_id

    def click(self, run_id: str, tab_id: str, selector: str) -> dict:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        try:
            page.click(selector, timeout=5000)
            return {"ok": True, "selector": selector}
        except Exception as exc:
            return {"ok": False, "selector": selector, "error": str(exc)}

    def fill(self, run_id: str, tab_id: str, selector: str, value: str) -> dict:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        try:
            page.fill(selector, value, timeout=5000)
            return {"ok": True, "selector": selector}
        except Exception as exc:
            return {"ok": False, "selector": selector, "error": str(exc)}

    def select_option(self, run_id: str, tab_id: str, selector: str, value: str) -> dict:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        try:
            page.select_option(selector, value, timeout=5000)
            return {"ok": True, "selector": selector, "value": value}
        except Exception as exc:
            return {"ok": False, "selector": selector, "error": str(exc)}

    def wait_for_selector(self, run_id: str, tab_id: str, selector: str, timeout_ms: int = 5000) -> dict:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            return {"ok": True, "found": True, "selector": selector}
        except Exception:
            return {"ok": True, "found": False, "selector": selector}

    def get_page_content(self, run_id: str, tab_id: str, max_chars: int = 4000) -> dict:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        result = page.evaluate("""() => ({
            url: location.href,
            title: document.title,
            text: document.body.innerText.slice(0, 4000),
            inputs: Array.from(document.querySelectorAll('input,textarea,select,button')).map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.type || null,
                name: el.name || null,
                id: el.id || null,
                placeholder: el.placeholder || null,
                text: el.textContent?.trim().slice(0, 80) || null,
            })),
            links: Array.from(document.querySelectorAll('a[href]')).map(a => a.href).slice(0, 30),
        })""")
        if isinstance(result, dict) and "text" in result:
            result["text"] = str(result["text"])[:max_chars]
        return result

    def capture_screenshot(self, run_id: str, tab_id: str, artifact_name: str) -> str:
        state = self._runs[run_id]
        page = state.pages[tab_id]
        path = self._artifacts_dir / run_id / artifact_name
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    def close(self) -> None:
        for run_id in list(self._runs):
            self.stop_run(run_id)
        self._browser.close()
        self._playwright.stop()


def _safe_eval_expression(script: str):
    """Evaluate simple numeric expressions for deterministic tests."""
    node = ast.parse(script, mode="eval")
    return _eval_ast(node.body)


def _eval_ast(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)):
        return node.value
    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    raise ValueError("Unsupported script for deterministic offline runtime")
