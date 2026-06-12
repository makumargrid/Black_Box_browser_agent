from __future__ import annotations

"""BIE Tier-1 http_post regression tests.

Locks the exploitation primitive: the agent must be able to POST attack payloads
(login SQLi, API injection) with a JSON body, accept the body under json/data/body
keys, and honor an explicit method override.
"""

from unittest.mock import MagicMock

from blackbox_service.bie import BIERequest, BrowserInteractionEngine


def _make_bie() -> BrowserInteractionEngine:
    return BrowserInteractionEngine(action_executor=MagicMock())


def test_http_post_sends_json_body(httpx_mock):
    """http_post with a json body reaches the server as a POST with that JSON."""
    captured = {}

    def _capture(request):
        import json as _json
        captured["method"] = request.method
        captured["body"] = _json.loads(request.content.decode())
        import httpx
        return httpx.Response(200, json={"authentication": {"token": "ey.fake.jwt"}})

    httpx_mock.add_callback(_capture, url="http://juice-shop:3000/rest/user/login")

    bie = _make_bie()
    out = bie.request(BIERequest(
        run_id="run-x",
        goal="test login SQLi",
        action_type="http_post",
        params={"url": "http://juice-shop:3000/rest/user/login",
                "json": {"email": "' OR 1=1--", "password": "x"}},
    ))

    assert out.ok is True
    assert out.tier_used == 1
    assert captured["method"] == "POST"
    assert captured["body"]["email"] == "' OR 1=1--"
    assert out.result["status_code"] == 200
    assert "token" in out.result["body_preview"]


def test_http_post_accepts_data_key(httpx_mock):
    """Body provided under 'data' (not 'json') is still sent — no silent drop."""
    captured = {}

    def _capture(request):
        import json as _json
        captured["body"] = _json.loads(request.content.decode())
        import httpx
        return httpx.Response(200, json={"ok": True})

    httpx_mock.add_callback(_capture, url="http://target/api/x")

    bie = _make_bie()
    out = bie.request(BIERequest(
        run_id="run-x", goal="", action_type="http_post",
        params={"url": "http://target/api/x", "data": {"q": "payload"}},
    ))
    assert out.ok is True
    assert captured["body"]["q"] == "payload"


def test_http_get_with_method_override_does_put(httpx_mock):
    """An explicit method param overrides the action_type (PUT/DELETE/PATCH support)."""
    captured = {}

    def _capture(request):
        captured["method"] = request.method
        import httpx
        return httpx.Response(200, text="ok")

    httpx_mock.add_callback(_capture, url="http://target/api/item/1")

    bie = _make_bie()
    out = bie.request(BIERequest(
        run_id="run-x", goal="", action_type="http_get",
        params={"url": "http://target/api/item/1", "method": "PUT", "json": {"a": 1}},
    ))
    assert out.ok is True
    assert captured["method"] == "PUT"
