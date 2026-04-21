from __future__ import annotations

import httpx

from blackbox_service.client import BlackboxClient


def test_http_client_create_run():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/runs"
        return httpx.Response(
            201,
            json={"run_id": "run-123", "status": "running", "targets": ["https://example.com"]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://testserver") as http_client:
        client = BlackboxClient(http_client=http_client)
        run = client.create_run(targets=["https://example.com"], options={})

    assert run["run_id"] == "run-123"
    assert run["status"] == "running"


def test_http_client_list_memory():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/runs/run-123/memory"
        return httpx.Response(
            200,
            json={"run_id": "run-123", "events": [{"event_id": "evt-1"}]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://testserver") as http_client:
        client = BlackboxClient(http_client=http_client)
        memory = client.list_memory("run-123")

    assert memory["run_id"] == "run-123"
    assert memory["events"][0]["event_id"] == "evt-1"
