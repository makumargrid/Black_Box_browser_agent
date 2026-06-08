from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)

# HexStrike AI v6.0 API paths (Flask server on :8888)
_HEALTH_PATH = "/health"

# FastMCP streamable-http endpoint (hexstrike_mcp.py on :8001)
_MCP_PATH = "/mcp"

# Port offset between Flask server and MCP server within the same Docker container.
# hexstrike_server.py = :8888, hexstrike_mcp.py = :8001
_MCP_PORT = 8001
_FLASK_PORT = 8888


def _mcp_url_from_base(base_url: str) -> str:
    """Derive the MCP server URL from the Flask base URL by swapping the port."""
    return base_url.rstrip("/").replace(f":{_FLASK_PORT}", f":{_MCP_PORT}")


class HexStrikeClient:
    """Transport layer for HexStrike AI v6.0.

    Uses the FastMCP streamable-http endpoint (POST /mcp) for tool discovery
    (tools/list) and tool execution (tools/call) via MCP JSON-RPC protocol.
    This gives 151 tools with full parameter schemas — zero hardcoding.

    Falls back to the Flask /health endpoint for tool list if the MCP server
    is not yet ready (e.g. during startup).
    """

    def __init__(self, base_url: str, timeout_s: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")         # Flask :8888
        self._mcp_url = _mcp_url_from_base(base_url)  # FastMCP :8001
        self._timeout_s = float(timeout_s)

    # ------------------------------------------------------------------
    # Health check (Flask server — used for reachability badge)
    # ------------------------------------------------------------------

    def health(self) -> bool:
        """Return True if HexStrike Flask server responds with 2xx on GET /health."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self._base_url}{_HEALTH_PATH}")
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.debug("HexStrike health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Tool discovery — MCP tools/list (primary) or /health fallback
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all available tool schemas from the MCP server.

        Calls MCP JSON-RPC tools/list → returns full schemas including
        parameter names, types, and defaults for all 151 tools.
        Falls back to /health (tool names only) if MCP server is not ready.
        """
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    f"{self._mcp_url}{_MCP_PATH}",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"content-type": "application/json", "accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            # MCP response shape: {"result": {"tools": [{name, description, inputSchema}, ...]}}
            tools = data.get("result", {}).get("tools", [])
            if tools:
                logger.debug("HexStrike list_tools: got %d tools via MCP", len(tools))
                return tools
        except Exception as exc:
            logger.debug("MCP list_tools failed, falling back to /health: %s", exc)

        return self._list_tools_health_fallback()

    def _list_tools_health_fallback(self) -> list[dict[str, Any]]:
        """Fallback: derive tool list from /health tools_status dict."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{self._base_url}{_HEALTH_PATH}")
                resp.raise_for_status()
                data = resp.json()
            return [
                {
                    "name": name,
                    "description": f"HexStrike security tool: {name}",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                }
                for name, available in data.get("tools_status", {}).items()
                if available
            ]
        except Exception as exc:
            logger.debug("HexStrike health fallback also failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Tool execution — MCP tools/call
    # ------------------------------------------------------------------

    def invoke(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool via MCP JSON-RPC tools/call.

        Routes automatically to the correct hexstrike endpoint — no hardcoded
        route mapping needed. The MCP server handles all tool dispatch internally.

        Return shape:
            {"ok": bool, "raw": ..., "stdout": str, "artifacts": list, "error": str|None}
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": params},
        }
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.post(
                    f"{self._mcp_url}{_MCP_PATH}",
                    json=payload,
                    headers={"content-type": "application/json", "accept": "application/json"},
                )
                if not (200 <= resp.status_code < 300):
                    return {
                        "ok": False, "raw": None, "stdout": "", "artifacts": [],
                        "error": f"MCP HTTP {resp.status_code}: {resp.text[:400]}",
                    }
                try:
                    data = resp.json()
                except Exception:
                    data = {"result": {"content": [{"type": "text", "text": resp.text}]}}

            # JSON-RPC error response
            if "error" in data:
                err = data["error"]
                err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                return {"ok": False, "raw": data, "stdout": "", "artifacts": [], "error": err_msg}

            # Successful MCP response: {"result": {"content": [{"type": "text", "text": "..."}]}}
            result = data.get("result", {})
            content = result.get("content", [])
            stdout = "\n".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ).strip()

            return {
                "ok": True,
                "raw": result,
                "stdout": stdout,
                "artifacts": [],
                "error": None,
            }

        except httpx.TimeoutException as exc:
            msg = f"MCP invoke timed out after {self._timeout_s}s: {exc}"
            logger.warning(msg)
            return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": msg}
        except Exception as exc:
            msg = f"MCP invoke error: {exc}"
            logger.warning(msg)
            return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": msg}
