from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from blackbox_service.agent import ALLOWED_ACTIONS, AgentDecision, Planner, RuleBasedPlanner
from blackbox_service.models import EventEnvelope, RunRecord, TabState
from blackbox_service.runtime import InMemoryRuntime, ThreadedPlaywrightRuntime
from blackbox_service.store import SQLiteEventStore
from blackbox_service.stream import RunEventBus


logger = logging.getLogger(__name__)


class RunNotFoundError(KeyError):
    pass


class BlackboxService:
    def __init__(
        self,
        db_path: str | Path = "blackbox_events.db",
        use_playwright: bool = False,
        browser_headless: bool = False,
        planner: Planner | None = None,
        artifacts_dir: str | Path = "artifacts",
        strict_playwright_runtime: bool = False,
    ) -> None:
        self._store = SQLiteEventStore(db_path=db_path)
        self._runtime = InMemoryRuntime(artifacts_dir=artifacts_dir)
        self._runtime_mode = "in_memory"
        self._runtime_warning: str | None = None
        if use_playwright:
            try:
                self._runtime = ThreadedPlaywrightRuntime(
                    headless=browser_headless,
                    artifacts_dir=artifacts_dir,
                )
                self._runtime_mode = "playwright"
            except Exception as exc:
                self._runtime_warning = str(exc)
                if strict_playwright_runtime:
                    raise RuntimeError(f"Playwright runtime unavailable: {exc}") from exc
                # Start service in degraded mode when browser dependencies are missing.
                logger.warning("Playwright runtime unavailable, falling back to in-memory runtime: %s", exc)
        self._bus = RunEventBus()
        self._use_playwright = use_playwright
        self._planner: Planner = planner or RuleBasedPlanner()
        self._agent_threads: dict[str, threading.Thread] = {}
        self._agent_state: dict[str, dict[str, Any]] = {}
        self._agent_lock = threading.Lock()

    def get_runtime_info(self) -> dict[str, Any]:
        return {
            "configured_use_playwright": self._use_playwright,
            "runtime_mode": self._runtime_mode,
            "runtime_warning": self._runtime_warning,
            "playwright_active": self._runtime_mode == "playwright",
        }

    def start_run(self, targets: list[str], options: dict[str, Any]) -> RunRecord:
        run = self._store.create_run(targets=targets, options=options)
        active_tab_id = self._runtime.start_run(run.run_id, targets)
        self._store.set_active_tab(run.run_id, active_tab_id)
        for tab in self._runtime.list_tabs(run.run_id):
            self._store.upsert_tab(tab)
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run.run_id,
                type="run.started",
                tab_id=active_tab_id,
                payload={"targets": targets, "options": options},
            )
        )
        loaded = self._store.get_run(run.run_id)
        assert loaded is not None
        return loaded

    def stop_run(self, run_id: str) -> RunRecord:
        self._ensure_run(run_id)
        stop_impl = getattr(self._runtime, "stop_run", None)
        if callable(stop_impl):
            stop_impl(run_id)
        self._store.set_run_status(run_id, status="stopped")
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="run.stopped",
                payload={},
            )
        )
        run = self._store.get_run(run_id)
        assert run is not None
        return run

    def start_agent(self, run_id: str, max_steps: int = 8, step_delay_ms: int = 400) -> dict[str, Any]:
        self._ensure_run(run_id)
        with self._agent_lock:
            current = self._agent_threads.get(run_id)
            if current is not None and current.is_alive():
                return dict(self._agent_state.get(run_id, {}))

            self._agent_state[run_id] = {
                "run_id": run_id,
                "status": "running",
                "steps_completed": 0,
                "max_steps": max_steps,
                "step_delay_ms": step_delay_ms,
                "last_error": None,
            }
            thread = threading.Thread(
                target=self.run_agent_steps,
                args=(run_id, max_steps, step_delay_ms),
                daemon=True,
                name=f"agent-loop-{run_id}",
            )
            self._agent_threads[run_id] = thread
            thread.start()
            return dict(self._agent_state[run_id])

    def get_agent_state(self, run_id: str) -> dict[str, Any]:
        self._ensure_run(run_id)
        with self._agent_lock:
            state = self._agent_state.get(run_id)
            if state is None:
                return {
                    "run_id": run_id,
                    "status": "idle",
                    "steps_completed": 0,
                    "max_steps": 0,
                    "step_delay_ms": 0,
                    "last_error": None,
                }
            return dict(state)

    def run_agent_steps(self, run_id: str, max_steps: int = 8, step_delay_ms: int = 400) -> dict[str, Any]:
        self._ensure_run(run_id)
        self._set_agent_state(
            run_id,
            {
                "run_id": run_id,
                "status": "running",
                "steps_completed": 0,
                "max_steps": max_steps,
                "step_delay_ms": step_delay_ms,
                "last_error": None,
            },
        )
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="agent.started",
                payload={"max_steps": max_steps, "step_delay_ms": step_delay_ms},
            )
        )

        status = "completed"
        last_error: str | None = None
        steps_completed = 0
        try:
            for step_index in range(max_steps):
                context = self._build_agent_context(run_id, step_index=step_index, max_steps=max_steps)
                decision = self._planner.next_decision(context)
                normalized = self._normalize_decision(run_id, decision)
                self._emit_agent_reasoning(run_id, step_index=step_index, decision=normalized)

                if normalized.done and normalized.action_type == "none":
                    break

                if normalized.action_type != "none":
                    action_out = self.execute_action(
                        run_id=run_id,
                        action_type=normalized.action_type,
                        params=normalized.params,
                    )
                    self._emit(
                        EventEnvelope(
                            event_id=f"evt-{uuid.uuid4().hex[:12]}",
                            run_id=run_id,
                            type="agent.step.completed",
                            tab_id=normalized.params.get("tab_id"),
                            payload={
                                "step_index": step_index,
                                "action_type": normalized.action_type,
                                "done": normalized.done,
                                "result_preview": str(action_out.get("result", ""))[:240],
                            },
                        )
                    )
                    steps_completed += 1

                if normalized.done:
                    break

                if step_delay_ms > 0:
                    time.sleep(step_delay_ms / 1000.0)

        except Exception as exc:
            status = "failed"
            last_error = str(exc)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="agent.failed",
                    payload={"error": last_error},
                )
            )

        final_state = {
            "run_id": run_id,
            "status": status,
            "steps_completed": steps_completed,
            "max_steps": max_steps,
            "step_delay_ms": step_delay_ms,
            "last_error": last_error,
        }
        self._set_agent_state(run_id, final_state)
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="agent.finished",
                payload=final_state,
            )
        )
        return final_state

    def get_run(self, run_id: str) -> RunRecord:
        run = self._store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def open_tab(
        self,
        run_id: str,
        url: str,
        correlation_id: str | None = None,
        parent_tab_id: str | None = None,
    ) -> TabState:
        self._ensure_run(run_id)
        tab = self._runtime.open_tab(
            run_id=run_id,
            url=url,
            correlation_id=correlation_id,
            parent_tab_id=parent_tab_id,
        )
        self._runtime.switch_tab(run_id, tab.tab_id)
        for current in self._runtime.list_tabs(run_id):
            self._store.upsert_tab(current)
        self._store.set_active_tab(run_id, tab.tab_id)
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="action.open_tab",
                tab_id=tab.tab_id,
                payload={
                    "url": tab.url,
                    "parent_tab_id": tab.parent_tab_id,
                    "correlation_id": tab.correlation_id,
                },
            )
        )
        return tab

    def list_tabs(self, run_id: str) -> list[TabState]:
        self._ensure_run(run_id)
        return self._store.list_tabs(run_id)

    def list_memory(self, run_id: str, limit: int = 500) -> list[EventEnvelope]:
        self._ensure_run(run_id)
        return self._store.list_events(run_id=run_id, limit=limit)

    def list_artifacts(self, run_id: str) -> dict[str, Any]:
        self._ensure_run(run_id)
        events = self._store.list_events(run_id, limit=2000)
        screenshot_events = [e for e in events if e.type == "artifact.screenshot"]
        return {
            "run_id": run_id,
            "artifact_count": len(screenshot_events),
            "artifacts": [e.payload for e in screenshot_events],
        }

    def stream_events(self, run_id: str):
        self._ensure_run(run_id)
        return self._bus.subscribe(run_id)

    def execute_action(self, run_id: str, action_type: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_run(run_id)
        if action_type == "open_tab":
            tab = self.open_tab(
                run_id=run_id,
                url=str(params["url"]),
                correlation_id=params.get("correlation_id"),
                parent_tab_id=params.get("parent_tab_id"),
            )
            return {"ok": True, "result": tab.model_dump(mode="json")}

        if action_type == "switch_tab":
            tab_id = str(params["tab_id"])
            self._runtime.switch_tab(run_id, tab_id)
            for tab in self._runtime.list_tabs(run_id):
                self._store.upsert_tab(tab)
            self._store.set_active_tab(run_id, tab_id)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.switch_tab",
                    tab_id=tab_id,
                    payload={},
                )
            )
            return {"ok": True, "result": {"active_tab_id": tab_id}}

        if action_type == "navigate":
            tab_id = str(params["tab_id"])
            url = str(params["url"])
            tab = self._runtime.navigate_tab(run_id, tab_id, url)
            self._store.upsert_tab(tab)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.navigate",
                    tab_id=tab_id,
                    payload={"url": url},
                )
            )
            return {"ok": True, "result": {"url": url}}

        if action_type == "eval_js":
            tab_id = str(params["tab_id"])
            script = str(params["script"])
            result = self._runtime.eval_js(run_id, tab_id, script)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.eval_js",
                    tab_id=tab_id,
                    payload={"script": script, "result": result},
                )
            )
            return {"ok": True, "result": result}

        if action_type == "inject_html":
            tab_id = str(params["tab_id"])
            html = str(params["html"])
            self._runtime.inject_html(run_id, tab_id, html)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.inject_html",
                    tab_id=tab_id,
                    payload={"html_length": len(html)},
                )
            )
            return {"ok": True, "result": {"html_length": len(html)}}

        if action_type == "read_console":
            tab_id = str(params["tab_id"])
            logs = self._runtime.get_console_logs(run_id, tab_id)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="observation.console",
                    tab_id=tab_id,
                    payload={"count": len(logs)},
                )
            )
            return {"ok": True, "result": logs}

        if action_type == "read_network":
            tab_id = str(params["tab_id"])
            net = self._runtime.get_network_events(run_id, tab_id)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="observation.network",
                    tab_id=tab_id,
                    payload={"count": len(net)},
                )
            )
            return {"ok": True, "result": net}

        if action_type == "snapshot":
            tab_id = str(params["tab_id"])
            file_name = f"{tab_id}-{uuid.uuid4().hex[:8]}.png"
            path = f"artifacts/{run_id}/{file_name}"
            capture_impl = getattr(self._runtime, "capture_screenshot", None)
            if callable(capture_impl):
                path = str(capture_impl(run_id, tab_id, file_name))
            artifact = {
                "kind": "screenshot",
                "tab_id": tab_id,
                "path": path,
            }
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="artifact.screenshot",
                    tab_id=tab_id,
                    payload=artifact,
                )
            )
            return {"ok": True, "result": artifact}

        if action_type == "click":
            tab_id = str(params["tab_id"])
            selector = str(params["selector"])
            result = self._runtime.click(run_id, tab_id, selector)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.click",
                    tab_id=tab_id,
                    payload={"selector": selector, "result": result},
                )
            )
            return {"ok": True, "result": result}

        if action_type == "fill":
            tab_id = str(params["tab_id"])
            selector = str(params["selector"])
            value = str(params.get("value", ""))
            result = self._runtime.fill(run_id, tab_id, selector, value)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.fill",
                    tab_id=tab_id,
                    payload={"selector": selector, "value": value},
                )
            )
            return {"ok": True, "result": result}

        if action_type == "select_option":
            tab_id = str(params["tab_id"])
            selector = str(params["selector"])
            value = str(params.get("value", ""))
            result = self._runtime.select_option(run_id, tab_id, selector, value)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.select_option",
                    tab_id=tab_id,
                    payload={"selector": selector, "value": value},
                )
            )
            return {"ok": True, "result": result}

        if action_type == "wait_for_selector":
            tab_id = str(params["tab_id"])
            selector = str(params["selector"])
            timeout_ms = int(params.get("timeout_ms", 5000))
            result = self._runtime.wait_for_selector(run_id, tab_id, selector, timeout_ms)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="action.wait_for_selector",
                    tab_id=tab_id,
                    payload={"selector": selector, "found": result.get("found")},
                )
            )
            return {"ok": True, "result": result}

        if action_type == "get_page_content":
            tab_id = str(params["tab_id"])
            result = self._runtime.get_page_content(run_id, tab_id)
            self._emit(
                EventEnvelope(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    type="observation.page_content",
                    tab_id=tab_id,
                    payload={"url": result.get("url"), "input_count": len(result.get("inputs", []))},
                )
            )
            return {"ok": True, "result": result}

        raise ValueError(f"Unsupported action_type: {action_type}")

    def _emit(self, event: EventEnvelope) -> None:
        self._store.append_event(event)
        self._bus.publish(event)

    def _emit_agent_reasoning(self, run_id: str, step_index: int, decision: AgentDecision) -> None:
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="agent.thought",
                payload={"step_index": step_index, "text": decision.thought},
            )
        )
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="agent.hypothesis",
                payload={"step_index": step_index, "text": decision.hypothesis},
            )
        )
        self._emit(
            EventEnvelope(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                type="agent.reasoning",
                payload={
                    "step_index": step_index,
                    "thought": decision.thought,
                    "hypothesis": decision.hypothesis,
                    "action_type": decision.action_type,
                    "params": decision.params,
                },
            )
        )

    def _normalize_decision(self, run_id: str, decision: AgentDecision) -> AgentDecision:
        active_tab_id = self._runtime.get_active_tab(run_id)
        if active_tab_id is None:
            tabs = self._runtime.list_tabs(run_id)
            if tabs:
                active_tab_id = tabs[0].tab_id
                self._runtime.switch_tab(run_id, active_tab_id)
                self._store.set_active_tab(run_id, active_tab_id)

        run = self.get_run(run_id)
        target_url = run.targets[0] if run.targets else "https://example.com"

        params = dict(decision.params)
        if decision.action_type in {
            "switch_tab", "navigate", "eval_js", "inject_html", "read_console", "read_network", "snapshot",
            "click", "fill", "select_option", "wait_for_selector", "get_page_content",
        }:
            params.setdefault("tab_id", active_tab_id)
        if decision.action_type in {"navigate", "open_tab"}:
            params.setdefault("url", target_url)
        if decision.action_type == "eval_js":
            params.setdefault("script", "({title: document.title, url: location.href})")
        if decision.action_type == "inject_html":
            params.setdefault("html", "<div data-blackbox='agent'>agent probe</div>")

        if params.get("tab_id") is None and decision.action_type != "open_tab":
            return AgentDecision(
                thought=decision.thought,
                hypothesis=decision.hypothesis,
                action_type="none",
                params={},
                done=True,
            )
        return AgentDecision(
            thought=decision.thought,
            hypothesis=decision.hypothesis,
            action_type=decision.action_type,
            params=params,
            done=decision.done,
        )

    def _build_agent_context(self, run_id: str, step_index: int, max_steps: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        tabs = [tab.model_dump(mode="json") for tab in self.list_tabs(run_id)]
        recent = [event.model_dump(mode="json") for event in self.list_memory(run_id, limit=25)][-12:]
        page_content: dict[str, Any] = {}
        active_tab_id = run.active_tab_id
        if active_tab_id:
            try:
                page_content = self._runtime.get_page_content(run_id, active_tab_id)
            except Exception:
                page_content = {"error": "could not retrieve page content"}
        return {
            "run": run.model_dump(mode="json"),
            "tabs": tabs,
            "active_tab_id": active_tab_id,
            "page_content": page_content,
            "recent_events": recent,
            "step_index": step_index,
            "max_steps": max_steps,
            "allowed_actions": sorted(ALLOWED_ACTIONS),
        }

    def _set_agent_state(self, run_id: str, state: dict[str, Any]) -> None:
        with self._agent_lock:
            self._agent_state[run_id] = dict(state)

    def _ensure_run(self, run_id: str) -> None:
        if self._store.get_run(run_id) is None:
            raise RunNotFoundError(run_id)
