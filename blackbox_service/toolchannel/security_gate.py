from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from blackbox_service.engagement_models import EngagementEvent, EngagementRecord, ToolInvocation
from blackbox_service.toolchannel.hexstrike_client import HexStrikeClient


# Rough per-tool cost estimates (USD) for budget tracking.
_TOOL_COST_MAP: dict[str, float] = {
    "nmap": 0.02,
    "nmap_scan": 0.02,
    "nuclei": 0.05,
    "nuclei_scan": 0.05,
    "ffuf": 0.02,
    "gobuster": 0.02,
    "subfinder": 0.01,
    "subfinder_enum": 0.01,
    "katana": 0.03,
    "katana_crawl": 0.03,
    "sqlmap": 0.10,
    "sqlmap_probe": 0.10,
}
_DEFAULT_TOOL_COST = 0.05

# Tools that require HITL approval before they can be executed.
_GATED_TOOLS: frozenset[str] = frozenset({"sqlmap", "sqlmap_probe", "metasploit", "exploit"})

# Host-level tools scan an entire host; port is irrelevant for scope decisions.
_HOST_LEVEL_TOOLS: frozenset[str] = frozenset({"nmap_scan", "nmap", "subfinder_enum", "subfinder"})
# URL-level tools target specific endpoints; explicit port must match the engagement port.
_URL_LEVEL_TOOLS: frozenset[str] = frozenset({
    "nuclei_scan", "nuclei", "katana_crawl", "katana",
    "sqlmap_probe", "sqlmap", "ffuf_discover", "ffuf",
    "gobuster_discover", "gobuster",
})

# Localhost aliases treated as equivalent for scope purposes.
_LOCALHOST_ALIASES: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def _scheme_default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _host_port(value: str) -> tuple[str, int | None]:
    """Parse a bare host, host:port, or full URL into (hostname, explicit_port|None).

    Returns ``port=None`` when no explicit port is present in the value so
    callers can distinguish "no port given" from "port 80 given".
    """
    value = value.strip()
    if not value:
        return ("", None)
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return (host, parsed.port)  # port is None when absent
    # No scheme — could be "host" or "host:port"
    if value.startswith("["):
        # IPv6 literal like [::1]:3000
        bracket_end = value.find("]")
        if bracket_end == -1:
            return (value.lower(), None)
        host = value[1:bracket_end].lower()
        rest = value[bracket_end + 1:]
        port = int(rest[1:]) if rest.startswith(":") else None
        return (host, port)
    parts = value.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return (parts[0].lower(), int(parts[1]))
    return (value.lower(), None)


def _hosts_equivalent(a: str, b: str) -> bool:
    """Return True if *a* and *b* name the same logical host.

    Treats localhost, 127.0.0.1, and ::1 as identical.
    """
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    # Normalise localhost aliases
    if a in _LOCALHOST_ALIASES and b in _LOCALHOST_ALIASES:
        return True
    return False


def _in_scope(tool: str, target: str, engagement_url: str) -> bool:
    """Return True if *target* is within the scope of *engagement_url* for *tool*.

    Scope rules:
    - Host-level tools (nmap, subfinder): only the hostname must match —
      port is irrelevant because these tools scan the whole machine.
    - URL-level tools (nuclei, katana, sqlmap, …): hostname must match AND
      any explicitly-stated port in *target* must equal the engagement port.
    - Unknown tools: apply URL-level rules (conservative).
    """
    try:
        eng_host, eng_port_or_none = _host_port(engagement_url)
        # Resolve the engagement's effective port (scheme default when absent).
        if eng_port_or_none is not None:
            eng_port = eng_port_or_none
        else:
            scheme = urlparse(engagement_url).scheme or "http"
            eng_port = _scheme_default_port(scheme)

        tgt_host, tgt_port = _host_port(target)

        if not _hosts_equivalent(tgt_host, eng_host):
            return False

        if tool in _HOST_LEVEL_TOOLS:
            # Port is irrelevant for host-level tools.
            return True

        # URL-level (or unknown) tool: explicit port must agree with engagement.
        if tgt_port is not None and tgt_port != eng_port:
            return False
        return True
    except Exception:
        return False


class SecurityToolGate:
    """The single, mandatory policy layer between agents and HexStrike.

    Every tool invocation flows through this gate. It enforces:
    1. SCOPE    — target must be within the engagement origin
    2. APPROVAL — gated tools (sqlmap etc.) require approval_granted == True
    3. BUDGET   — combined tool spend must not exceed engagement limit or hard cap
    4. CLEANUP  — artifact paths are registered before execution for crash recovery
    5. AUDIT    — every decision (pass or reject) appends an EngagementEvent

    No agent ever calls HexStrikeClient directly. This gate is the product's
    legal and operational safety boundary.
    """

    def __init__(
        self,
        client: HexStrikeClient,
        engagement: EngagementRecord,
        artifacts_dir: str | Path,
        logger: logging.Logger,
        budget_hard_cap_usd: float = 5.0,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._client = client
        self._engagement = engagement
        self._artifacts_dir = Path(artifacts_dir)
        self._log = logger
        self._hard_cap = float(budget_hard_cap_usd)
        self._lock = threading.Lock()
        # Maps pending_key -> expected artifact path for crash-recovery cleanup.
        self._pending: dict[str, str] = {}
        # Optional event sink: if set, all audit events are published through it
        # (which appends to rec.events AND publishes to the EngagementEventBus for
        # live SSE delivery). If None, events are appended directly to rec.events.
        self._event_sink = event_sink

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def invoke(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """Invoke *tool* with *params* after enforcing all policy checks.

        Returns the same normalized dict shape as HexStrikeClient.invoke().
        All rejection paths return ok=False and record an audit event.
        """
        target = str(params.get("target", ""))
        est_cost = _TOOL_COST_MAP.get(tool, _DEFAULT_TOOL_COST)

        # 1. SCOPE CHECK
        if not _in_scope(tool, target, self._engagement.target_url):
            reason = f"out_of_scope: target={target!r} not within {self._engagement.target_url!r}"
            self._reject_event(tool, target, reason)
            self._log.warning("SecurityToolGate SCOPE REJECT tool=%s target=%s", tool, target)
            return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": "out_of_scope"}

        # 2. APPROVAL CHECK (for gated tools)
        if tool in _GATED_TOOLS and not self._engagement.approval_granted:
            reason = "requires_hitl_approval"
            self._reject_event(tool, target, reason)
            self._log.warning("SecurityToolGate APPROVAL REJECT tool=%s", tool)
            return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": reason}

        # 3. BUDGET CHECK (atomic: check + decrement under lock)
        # Tool spend is tracked in a SEPARATE pool (engagement.tool_spent_usd)
        # so tool costs do not compete with LLM/browser engagement costs.
        # The hard_cap is the ceiling for the tool pool only.
        with self._lock:
            if self._engagement.tool_spent_usd + est_cost > self._hard_cap:
                reason = (
                    f"budget_exhausted: tool_spent={self._engagement.tool_spent_usd:.4f} "
                    f"est={est_cost:.4f} hard_cap={self._hard_cap:.4f}"
                )
                self._reject_event(tool, target, reason)
                self._log.warning("SecurityToolGate BUDGET REJECT tool=%s", tool)
                return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": "budget_exhausted"}
            # Reserve the tool-budget slot atomically.
            self._engagement.tool_spent_usd += est_cost

        # 4. PRE-CREATE ToolInvocation (cleanup record in case of crash/kill)
        invocation = ToolInvocation(
            tool_name=tool,
            target=target,
            args={k: v for k, v in params.items() if k != "target"},
            started_at=datetime.now(timezone.utc),
        )
        self._engagement.tool_invocations.append(invocation)

        # 5. REGISTER expected artifact path for cleanup tracking
        artifact_dir = self._artifacts_dir / self._engagement.engagement_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        expected_artifact = str(artifact_dir / f"{tool}_{int(time.time())}.out")
        pending_key = f"{tool}:{invocation.started_at.isoformat()}"
        with self._lock:
            self._pending[pending_key] = expected_artifact

        self._log.info("SecurityToolGate INVOKE tool=%s target=%s cost=%.4f", tool, target, est_cost)

        # 6. EXECUTE via client (timed)
        t0 = time.monotonic()
        result = self._client.invoke(tool, params)
        duration_ms = (time.monotonic() - t0) * 1000.0

        # 7. UPDATE ToolInvocation record
        invocation.completed_at = datetime.now(timezone.utc)
        invocation.duration_ms = round(duration_ms, 1)
        invocation.ok = bool(result.get("ok", False))
        invocation.cost_usd = est_cost
        invocation.artifacts = list(result.get("artifacts", []))
        invocation.error = result.get("error")

        # Deregister from pending (artifact is complete or never created).
        # On failure: best-effort remove the expected artifact file and refund budget.
        with self._lock:
            self._pending.pop(pending_key, None)

        if not invocation.ok:
            try:
                Path(expected_artifact).unlink(missing_ok=True)
            except Exception:
                pass
            with self._lock:
                self._engagement.tool_spent_usd = max(
                    0.0, self._engagement.tool_spent_usd - est_cost
                )

        # 8. AUDIT EVENT
        self._emit(
            "tool.invoked",
            {
                "tool": tool,
                "ok": invocation.ok,
                "cost_usd": est_cost,
                "duration_ms": invocation.duration_ms,
                "target": target,
                "error": invocation.error,
            },
        )

        return result

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an audit event through the event_sink (for live SSE) or directly.

        When an event_sink is configured (set by the orchestrator), the sink
        calls both rec.events.append AND EngagementEventBus.publish so the
        Ops Console receives tool events in real time over SSE.

        Without a sink (standalone gate in unit tests), events are appended
        directly to rec.events.
        """
        if self._event_sink is not None:
            self._event_sink(event_type, payload)
        else:
            self._engagement.events.append(
                EngagementEvent(type=event_type, payload=payload)
            )

    def cleanup(self) -> None:
        """Remove any orphaned artifact files left by in-flight tool invocations.

        Called from the orchestrator's ``finally`` block on run completion,
        failure, or approval-pause so that crashed or killed tools never leave
        stale files on disk.  Best-effort and exception-safe — never raises.
        """
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
        for key, path in pending.items():
            try:
                p = Path(path)
                if p.exists():
                    p.unlink()
                    self._log.info("SecurityToolGate cleanup: removed orphaned artifact %s", path)
            except Exception as exc:
                self._log.debug("SecurityToolGate cleanup error for %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _reject_event(self, tool: str, target: str, reason: str) -> None:
        self._emit(
            "tool.rejected",
            {"tool": tool, "target": target, "reason": reason},
        )
