from __future__ import annotations

from blackbox_service.toolchannel.hexstrike_client import HexStrikeClient


def test_health_unreachable_returns_false():
    """health() must return False (not raise) when the server is unreachable."""
    client = HexStrikeClient(base_url="http://127.0.0.1:19999", timeout_s=2.0)
    result = client.health()
    assert result is False


def test_invoke_unreachable_returns_ok_false_with_error():
    """invoke() must return ok=False + error string (not raise) when unreachable."""
    client = HexStrikeClient(base_url="http://127.0.0.1:19999", timeout_s=2.0)
    result = client.invoke("nmap", {"target": "example.com"})
    assert result["ok"] is False
    assert isinstance(result["error"], str)
    assert len(result["error"]) > 0
    assert result["artifacts"] == []
    assert result["stdout"] == ""


def test_list_tools_unreachable_returns_empty_list():
    """list_tools() must return [] (not raise) when server is unreachable."""
    client = HexStrikeClient(base_url="http://127.0.0.1:19999", timeout_s=2.0)
    result = client.list_tools()
    assert result == []


def test_invoke_normalizes_response(httpx_mock):
    """invoke() parses a successful HexStrike response into the normalized shape."""
    from pytest_httpx import HTTPXMock  # type: ignore[import]

    httpx_mock.add_response(
        method="POST",
        url="http://hexstrike-test:8888/tools/execute",
        json={
            "stdout": "Nmap scan report for example.com",
            "artifacts": ["nmap_out.xml"],
        },
        status_code=200,
    )
    client = HexStrikeClient(base_url="http://hexstrike-test:8888", timeout_s=10.0)
    result = client.invoke("nmap", {"target": "example.com"})
    assert result["ok"] is True
    assert result["stdout"] == "Nmap scan report for example.com"
    assert result["artifacts"] == ["nmap_out.xml"]
    assert result["error"] is None


def test_invoke_list_response_ok_true(httpx_mock):
    """A 2xx response whose body is a JSON list → ok=True, raw is the list, stdout=''."""
    from pytest_httpx import HTTPXMock  # type: ignore[import]

    findings = [
        {"template_id": "cve-2021-44228", "severity": "critical", "matched_at": "http://example.com/login"},
        {"template_id": "exposed-metrics", "severity": "medium", "matched_at": "http://example.com/metrics"},
    ]
    httpx_mock.add_response(
        method="POST",
        url="http://hexstrike-test:8888/tools/execute",
        json=findings,
        status_code=200,
    )
    client = HexStrikeClient(base_url="http://hexstrike-test:8888", timeout_s=10.0)
    result = client.invoke("nuclei", {"target": "http://example.com"})

    assert result["ok"] is True, f"Expected ok=True for list response, got: {result['error']}"
    assert result["raw"] == findings
    assert result["stdout"] == ""
    assert result["artifacts"] == []
    assert result["error"] is None


def test_invoke_string_body_response_ok_true(httpx_mock):
    """A 2xx response with a plain string body → ok=True, stdout=str(body)."""
    from pytest_httpx import HTTPXMock  # type: ignore[import]
    import json

    httpx_mock.add_response(
        method="POST",
        url="http://hexstrike-test:8888/tools/execute",
        content=b"scan completed successfully",
        status_code=200,
    )
    client = HexStrikeClient(base_url="http://hexstrike-test:8888", timeout_s=10.0)
    result = client.invoke("subfinder", {"target": "example.com"})

    # Non-JSON body is wrapped: raw = {"body": resp.text}
    assert result["ok"] is True
    assert result["error"] is None
