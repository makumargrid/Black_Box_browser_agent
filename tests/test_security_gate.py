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
    tool_spent: float = 0.0,
    approval_granted: bool = False,
) -> EngagementRecord:
    rec = EngagementRecord(engagement_id="eng-test", target_url=target_url)
    rec.budget = BudgetState(limit_usd=budget_limit, spent_usd=spent)
    rec.tool_spent_usd = tool_spent
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
    """When tool_spent + cost > hard_cap → rejected with budget_exhausted, event recorded."""
    rec = _make_record(target_url="http://example.com", tool_spent=4.99)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=5.0)

    # nmap_scan costs 0.02 → 4.99 + 0.02 = 5.01 > 5.0
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
    assert rec.tool_spent_usd == 0.02
    assert "tool.invoked" in _event_types(rec)


def test_hard_cap_takes_precedence_over_budget_limit(tmp_path):
    """hard_cap is respected even when the engagement budget limit is larger."""
    # budget limit = 100, but tool hard_cap = 5 → tools cap at 5 regardless
    rec = _make_record(target_url="http://example.com", budget_limit=100.0, tool_spent=4.98)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=5.0)

    # nmap costs 0.02 → 4.98 + 0.02 = 5.00 <= 5.0 (exactly at cap, allowed)
    result = gate.invoke("nmap_scan", {"target": "http://example.com"})
    assert result["ok"] is True

    # Now spent = 5.00; another nmap (0.02) → 5.02 > 5.0 → rejected
    result2 = gate.invoke("nmap_scan", {"target": "http://example.com"})
    assert result2["ok"] is False
    assert result2["error"] == "budget_exhausted"


def test_two_concurrent_near_budget_calls_exactly_one_rejected(tmp_path):
    """Concurrent calls near tool hard_cap: exactly one succeeds, one is rejected (no double-spend)."""
    # tool hard_cap = 5.0, tool_spent = 4.98 → exactly 0.02 remaining.
    # nmap costs 0.02 → only one of two concurrent calls should pass.
    rec = _make_record(target_url="http://example.com", budget_limit=100.0, tool_spent=4.98)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=5.0)

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


def test_large_engagement_budget_still_caps_at_hard_cap(tmp_path):
    """A large engagement budget ($50) does not bypass the tool hard_cap ($5)."""
    rec = _make_record(target_url="http://example.com", budget_limit=50.0)
    gate, fake_client = _make_gate(rec, tmp_path=tmp_path, hard_cap=5.0)

    # nmap_scan costs 0.02 each; 250 calls would hit 5.0 exactly.
    # Run until first rejection.
    last_ok_tool_spent = 0.0
    rejected = False
    for _ in range(260):
        r = gate.invoke("nmap_scan", {"target": "http://example.com"})
        if not r["ok"]:
            rejected = True
            break
        last_ok_tool_spent = rec.tool_spent_usd

    assert rejected, "Tool calls were never rejected despite hard_cap=$5"
    assert last_ok_tool_spent <= 5.0, f"tool_spent_usd exceeded hard_cap: {last_ok_tool_spent}"
    # Global engagement budget must be untouched by tool tracking
    assert rec.budget.spent_usd == 0.0, "Global engagement budget was erroneously modified"


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


# ---------------------------------------------------------------------------
# FIX 1 tests — tool-aware scope check
# ---------------------------------------------------------------------------

import pytest

@pytest.mark.parametrize("engagement_url,tool,target,expect_in_scope", [
    # Proven failing case: bare hostname for nmap (host-level) must pass
    ("http://juice-shop:3000/#/", "nmap_scan",      "juice-shop",                True),
    ("http://juice-shop:3000/#/", "nmap_scan",      "juice-shop:3000",           True),
    ("http://juice-shop:3000/#/", "subfinder_enum", "juice-shop",                True),
    # URL-level tools: full URL with matching port must pass
    ("http://juice-shop:3000/#/", "nuclei_scan",    "http://juice-shop:3000",    True),
    # URL-level tool with wrong port must be rejected
    ("http://juice-shop:3000/#/", "nuclei_scan",    "http://juice-shop:9999",    False),
    # Different host must be rejected regardless of tool type
    ("http://juice-shop:3000/#/", "nmap_scan",      "evil.com",                  False),
    # localhost <-> 127.0.0.1 equivalence for host-level tool
    ("http://localhost:3000",     "nmap_scan",      "127.0.0.1",                 True),
    # localhost <-> 127.0.0.1 for URL-level with correct port
    ("http://localhost:3000",     "nuclei_scan",    "http://127.0.0.1:3000",     True),
    # URL-level: no explicit port in target should be allowed (defaults to engagement port)
    ("http://juice-shop:3000/#/", "katana_crawl",   "http://juice-shop",         True),
    # Case insensitivity
    ("http://Juice-Shop:3000",    "nmap_scan",      "juice-shop",                True),
])
def test_scope_rules_table(engagement_url, tool, target, expect_in_scope, tmp_path):
    """Parametrized scope truth table covering the proven-failing nmap bare-host case."""
    from blackbox_service.toolchannel.security_gate import _in_scope
    result = _in_scope(tool, target, engagement_url)
    assert result is expect_in_scope, (
        f"_in_scope({tool!r}, {target!r}, {engagement_url!r}) = {result}, "
        f"expected {expect_in_scope}"
    )


def test_gate_accepts_bare_host_nmap_via_invoke(tmp_path):
    """End-to-end: SecurityToolGate.invoke must NOT reject bare-host nmap_scan."""
    import logging
    from blackbox_service.engagement_models import BudgetState, EngagementRecord
    from blackbox_service.toolchannel.security_gate import SecurityToolGate
    from unittest.mock import MagicMock

    rec = EngagementRecord(
        engagement_id="eng-scope-test",
        target_url="http://juice-shop:3000/#/",
    )
    rec.budget = BudgetState(limit_usd=100.0)

    fake_client = MagicMock()
    fake_client.invoke.return_value = {
        "ok": True, "raw": {}, "stdout": "nmap output", "artifacts": [], "error": None
    }
    gate = SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=tmp_path,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=100.0,
    )

    result = gate.invoke("nmap_scan", {"target": "juice-shop"})
    assert result["ok"] is True, (
        f"Bare-host nmap_scan was rejected: {result.get('error')}"
    )
    rejected = [e for e in rec.events if e.type == "tool.rejected"]
    assert len(rejected) == 0, f"Unexpected tool.rejected events: {rejected}"
