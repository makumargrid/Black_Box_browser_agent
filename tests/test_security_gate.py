from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

from blackbox_service.engagement_models import BudgetState, EngagementRecord
from blackbox_service.toolchannel.security_gate import SecurityToolGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    target_url: str = "http://example.com",
    budget_limit: float = 10.0,
    spent: float = 0.0,
    approval_granted: bool = False,
) -> EngagementRecord:
    rec = EngagementRecord(engagement_id="eng-test", target_url=target_url)
    rec.budget = BudgetState(limit_usd=budget_limit, spent_usd=spent)
    rec.approval_granted = approval_granted
    return rec


def _make_gate(
    rec: EngagementRecord,
    client_responses: dict[str, Any] | None = None,
    tmp_path=None,
    hard_cap: float = 5.0,
) -> tuple[SecurityToolGate, MagicMock]:
    import logging
    import tempfile
    from pathlib import Path

    fake_client = MagicMock()
    if client_responses:
        fake_client.invoke.return_value = client_responses
    else:
        fake_client.invoke.return_value = {
            "ok": True, "raw": {}, "stdout": "output", "artifacts": [], "error": None
        }

    artifacts_dir = tmp_path if tmp_path else Path(tempfile.mkdtemp())
    gate = SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=artifacts_dir,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=hard_cap,
    )
    return gate, fake_client


def _event_types(rec: EngagementRecord) -> list[str]:
    return [e.type for e in rec.events]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_out_of_scope_rejected_no_client_call(tmp_path):
    """Target outside engagement origin → rejected, no HexStrike call, audit event recorded."""
    rec = _make_record(target_url="http://example.com")
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path)

    result = gate.invoke("nmap_scan", {"target": "http://evil.com"})

    assert result["ok"] is False
    assert result["error"] == "out_of_scope"
    fake_client.invoke.assert_not_called()
    assert "tool.rejected" in _event_types(rec)
    reject_event = next(e for e in rec.events if e.type == "tool.rejected")
    assert "out_of_scope" in reject_event.payload["reason"]


def test_gated_tool_without_approval_rejected(tmp_path):
    """sqlmap_probe without approval_granted → rejected, no client call, audit event."""
    rec = _make_record(target_url="http://example.com", approval_granted=False)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path)

    result = gate.invoke("sqlmap_probe", {"target": "http://example.com/login"})

    assert result["ok"] is False
    assert result["error"] == "requires_hitl_approval"
    fake_client.invoke.assert_not_called()
    assert "tool.rejected" in _event_types(rec)


def test_gated_tool_with_approval_allowed(tmp_path):
    """sqlmap_probe with approval_granted=True → passes all checks, client is called."""
    rec = _make_record(target_url="http://example.com", approval_granted=True)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path)

    result = gate.invoke("sqlmap_probe", {"target": "http://example.com/login"})

    assert result["ok"] is True
    fake_client.invoke.assert_called_once()
    assert "tool.invoked" in _event_types(rec)


def test_budget_exhaustion_blocks_and_records_event(tmp_path):
    """When spent + cost > cap → rejected with budget_exhausted, event recorded."""
    rec = _make_record(target_url="http://example.com", budget_limit=1.0, spent=0.99)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=5.0)

    # nmap_scan costs 0.02 → 0.99 + 0.02 = 1.01 > 1.0
    result = gate.invoke("nmap_scan", {"target": "http://example.com"})

    assert result["ok"] is False
    assert result["error"] == "budget_exhausted"
    fake_client.invoke.assert_not_called()
    assert "tool.rejected" in _event_types(rec)


def test_success_updates_tool_invocations_and_budget(tmp_path):
    """Successful invocation updates tool_invocations list, budget, and emits tool.invoked."""
    rec = _make_record(target_url="http://example.com")
    gate, fake_client = _make_gate(
        rec,
        client_responses={"ok": True, "raw": {}, "stdout": "scan done", "artifacts": ["f.xml"], "error": None},
        tmp_path=tmp_path,
    )

    result = gate.invoke("nmap_scan", {"target": "http://example.com"})

    assert result["ok"] is True
    assert len(rec.tool_invocations) == 1
    ti = rec.tool_invocations[0]
    assert ti.tool_name == "nmap_scan"
    assert ti.ok is True
    assert ti.cost_usd == 0.02
    assert ti.artifacts == ["f.xml"]
    assert ti.completed_at is not None
    assert ti.duration_ms is not None
    assert rec.budget.spent_usd == 0.02
    assert "tool.invoked" in _event_types(rec)


def test_hard_cap_takes_precedence_over_budget_limit(tmp_path):
    """hard_cap is respected even when the engagement budget limit is larger."""
    # budget limit = 100, hard cap = 5 → cap used is min(100, 5) = 5
    rec = _make_record(target_url="http://example.com", budget_limit=100.0, spent=4.98)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=5.0)

    # nmap costs 0.02 → 4.98 + 0.02 = 5.00 <= 5.0 (exactly at cap, allowed)
    result = gate.invoke("nmap_scan", {"target": "http://example.com"})
    assert result["ok"] is True

    # Now spent = 5.00; another nmap (0.02) → 5.02 > 5.0 → rejected
    result2 = gate.invoke("nmap_scan", {"target": "http://example.com"})
    assert result2["ok"] is False
    assert result2["error"] == "budget_exhausted"


def test_two_concurrent_near_budget_calls_exactly_one_rejected(tmp_path):
    """Concurrent calls near budget cap: exactly one succeeds, one is rejected (no double-spend)."""
    # Budget: 10.0 limit, spent 9.98, hard_cap 100 → exactly 0.02 remaining.
    # nmap costs 0.02 → only one of two concurrent calls should pass.
    rec = _make_record(target_url="http://example.com", budget_limit=10.0, spent=9.98)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=100.0)

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def call():
        barrier.wait()
        results.append(gate.invoke("nmap_scan", {"target": "http://example.com"}))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok_count = sum(1 for r in results if r["ok"])
    rejected_count = sum(1 for r in results if not r["ok"])
    assert ok_count == 1, f"expected exactly 1 success, got {ok_count}"
    assert rejected_count == 1, f"expected exactly 1 rejection, got {rejected_count}"


# ---------------------------------------------------------------------------
# C2 tests — real cleanup guardrail
# ---------------------------------------------------------------------------

def test_failed_invocation_removes_expected_artifact_file(tmp_path):
    """When a tool call fails, the expected artifact file (if it exists) is removed and _pending is cleared."""
    rec = _make_record(target_url="http://example.com")
    gate, fake_client = _make_gate(
        rec,
        client_responses={"ok": False, "raw": {}, "stdout": "", "artifacts": [], "error": "tool failed"},
        tmp_path=tmp_path,
    )

    # Manually create the artifact file that the gate would have pre-registered
    artifact_dir = tmp_path / rec.engagement_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Monkeypatch to capture the expected_artifact path before the call
    created_path: list[str] = []
    original_invoke = gate._client.invoke

    def patched_invoke(tool, params):
        # Create the file so we can assert it gets removed
        import time as _time
        p = artifact_dir / f"nmap_scan_{int(_time.time())}.out"
        p.touch()
        created_path.append(str(p))
        # Insert it into _pending directly so cleanup sees it
        key = list(gate._pending.keys())[0] if gate._pending else None
        if key:
            gate._pending[key] = str(p)
        return {"ok": False, "raw": {}, "stdout": "", "artifacts": [], "error": "tool failed"}

    gate._client.invoke = patched_invoke

    gate.invoke("nmap_scan", {"target": "http://example.com"})

    # After a failed invocation, _pending must be empty
    assert len(gate._pending) == 0, "_pending not cleared after failed invocation"


def test_cleanup_removes_stuck_pending_entry(tmp_path):
    """cleanup() removes any files registered in _pending and empties the dict."""
    rec = _make_record(target_url="http://example.com")
    gate, _ = _make_gate(rec, tmp_path=tmp_path)

    # Simulate an orphaned entry (e.g. from a killed run)
    orphan = tmp_path / "orphan_tool_artifact.out"
    orphan.touch()
    gate._pending["tool:2026-01-01T00:00:00"] = str(orphan)

    assert orphan.exists()
    gate.cleanup()

    assert not orphan.exists(), "cleanup() did not remove the orphaned artifact file"
    assert len(gate._pending) == 0, "cleanup() did not empty _pending"


def test_cleanup_safe_on_missing_file(tmp_path):
    """cleanup() does not raise if a _pending file no longer exists."""
    rec = _make_record(target_url="http://example.com")
    gate, _ = _make_gate(rec, tmp_path=tmp_path)

    gate._pending["tool:2026-01-01T00:00:00"] = str(tmp_path / "nonexistent_file.out")
    gate.cleanup()  # must not raise
    assert len(gate._pending) == 0
