from __future__ import annotations

from typing import Any

import httpx


class BlackboxClient:
    """HTTP client adapter for Pen_test_v2 or other orchestrators."""

    def __init__(self, base_url: str = "http://localhost:8080", http_client: httpx.Client | None = None) -> None:
        self._external_client = http_client
        self._client = http_client or httpx.Client(base_url=base_url)

    def close(self) -> None:
        if self._external_client is None:
            self._client.close()

    def create_run(self, targets: list[str], options: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/runs", json={"targets": targets, "options": options})
        response.raise_for_status()
        return response.json()

    def run_action(self, run_id: str, action_type: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"/runs/{run_id}/actions",
            json={"action_type": action_type, "params": params},
        )
        response.raise_for_status()
        return response.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    def list_tabs(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/runs/{run_id}/tabs")
        response.raise_for_status()
        return response.json()

    def list_memory(self, run_id: str, limit: int = 500) -> dict[str, Any]:
        response = self._client.get(f"/runs/{run_id}/memory", params={"limit": limit})
        response.raise_for_status()
        return response.json()

    def list_artifacts(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/runs/{run_id}/artifacts")
        response.raise_for_status()
        return response.json()

    def start_agent(self, run_id: str, max_steps: int = 8, step_delay_ms: int = 400) -> dict[str, Any]:
        response = self._client.post(
            f"/runs/{run_id}/agent/start",
            json={"max_steps": max_steps, "step_delay_ms": step_delay_ms},
        )
        response.raise_for_status()
        return response.json()

    def get_agent_state(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/runs/{run_id}/agent/state")
        response.raise_for_status()
        return response.json()
