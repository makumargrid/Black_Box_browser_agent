from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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


def _same_origin(a: str, b: str) -> bool:
    """Return True if URL *a* is within the same origin (host+port) as URL *b*."""
    try:
        pa = urlparse(a if "://" in a else f"http://{a}")
        pb = urlparse(b if "://" in b else f"http://{b}")
        return pa.hostname == pb.hostname and (pa.port or 80) == (pb.port or 80)
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
    ) -> None:
        self._client = client
        self._engagement = engagement
        self._artifacts_dir = Path(artifacts_dir)
        self._log = logger
        self._hard_cap = float(budget_hard_cap_usd)
        self._lock = threading.Lock()
        # Maps pending_key -> expected artifact path for crash-recovery cleanup.
        self._pending: dict[str, str] = {}

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
        if not _same_origin(target, self._engagement.target_url):
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
        with self._lock:
            effective_cap = min(self._engagement.budget.limit_usd, self._hard_cap)
            if self._engagement.budget.spent_usd + est_cost > effective_cap:
                reason = (
                    f"budget_exhausted: spent={self._engagement.budget.spent_usd:.4f} "
                    f"est={est_cost:.4f} cap={effective_cap:.4f}"
                )
                self._reject_event(tool, target, reason)
                self._log.warning("SecurityToolGate BUDGET REJECT tool=%s", tool)
                return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": "budget_exhausted"}
            # Reserve the budget slot before releasing the lock so concurrent
            # calls cannot both pass the same check.
            self._engagement.budget.spent_usd += est_cost

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
                self._engagement.budget.spent_usd = max(
                    0.0, self._engagement.budget.spent_usd - est_cost
                )

        # 8. AUDIT EVENT
        self._engagement.events.append(
            EngagementEvent(
                type="tool.invoked",
                payload={
                    "tool": tool,
                    "ok": invocation.ok,
                    "cost_usd": est_cost,
                    "duration_ms": invocation.duration_ms,
                    "target": target,
                    "error": invocation.error,
                },
            )
        )

        return result

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
        self._engagement.events.append(
            EngagementEvent(
                type="tool.rejected",
                payload={"tool": tool, "target": target, "reason": reason},
            )
        )
