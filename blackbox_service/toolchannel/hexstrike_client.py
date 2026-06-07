from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)

# HexStrike AI v6.0 API paths.
# Verified against upstream README at github.com/0x4m4/hexstrike-ai:
#   Health check:   GET  /health
#   Tool execution: POST /tools/execute  (FastMCP HTTP server, payload: {"tool": str, "params": dict})
_HEALTH_PATH = "/health"
_EXECUTE_PATH = "/tools/execute"


class HexStrikeClient:
    """Thin HTTP transport layer for the HexStrike AI v6.0 tool server.

    Contract: no method ever raises an exception to the caller — all errors
    produce a structured negative result. This allows the SecurityToolGate to
    handle errors without try/except boilerplate.
    """

    def __init__(self, base_url: str, timeout_s: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)

    def health(self) -> bool:
        """Return True if HexStrike responds to GET /health with 2xx status."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self._base_url}{_HEALTH_PATH}")
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.debug("HexStrike health check failed: %s", exc)
            return False

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of available tools from HexStrike (best-effort, empty on error)."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{self._base_url}/tools")
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return data
                return list(data.get("tools", []))
        except Exception as exc:
            logger.debug("HexStrike list_tools failed: %s", exc)
            return []

    def invoke(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool on HexStrike and return a normalized result dict.

        Return shape::

            {
                "ok": bool,
                "raw": <full server payload or None>,
                "stdout": str,
                "artifacts": list[str],
                "error": str | None,
            }

        Never raises — all errors set ``ok=False`` with a descriptive ``error`` string.
        """
        payload = {"tool": tool, "params": params}
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.post(
                    f"{self._base_url}{_EXECUTE_PATH}",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
                if not (200 <= resp.status_code < 300):
                    return {
                        "ok": False,
                        "raw": None,
                        "stdout": "",
                        "artifacts": [],
                        "error": f"HexStrike HTTP {resp.status_code}: {resp.text[:400]}",
                    }
                try:
                    raw = resp.json()
                except Exception:
                    raw = {"body": resp.text}

                # Normalize to the expected output shape regardless of JSON structure.
                # Real tools (e.g. nuclei) may return a JSON list of findings.
                if isinstance(raw, dict):
                    stdout = str(raw.get("stdout", raw.get("output", "")))
                    artifacts = list(raw.get("artifacts", []))
                elif isinstance(raw, list):
                    # List of findings; downstream parsers handle this shape via raw.
                    stdout = ""
                    artifacts = []
                else:
                    # Scalar/string response — treat as stdout text.
                    stdout = str(raw)
                    artifacts = []

                return {
                    "ok": True,
                    "raw": raw,
                    "stdout": stdout,
                    "artifacts": artifacts,
                    "error": None,
                }
        except httpx.TimeoutException as exc:
            msg = f"HexStrike invoke timed out after {self._timeout_s}s: {exc}"
            logger.warning(msg)
            return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": msg}
        except Exception as exc:
            msg = f"HexStrike invoke error: {exc}"
            logger.warning(msg)
            return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": msg}
