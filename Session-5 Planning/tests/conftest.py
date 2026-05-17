"""
Shared test fixtures and helpers.

Spins up the mock gateway on a free port for each test session.
Monkey-patches the e-Sankhyiki MCP client to return canned data without
hitting the network.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from typing import Any

import httpx
import uvicorn

from tests.mock_gateway import app as mock_gateway_app, find_free_port


# -------------------------------------------------------------------------
# Mock gateway lifecycle
# -------------------------------------------------------------------------


class _MockGatewayServer:
    """Run uvicorn in a background thread."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.config = uvicorn.Config(
            mock_gateway_app, host="127.0.0.1", port=port, log_level="error"
        )
        self.server = uvicorn.Server(self.config)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        # Wait for the server to become reachable.
        for _ in range(50):
            try:
                with httpx.Client(timeout=0.5) as client:
                    r = client.get(f"http://127.0.0.1:{self.port}/")
                    if r.status_code == 200:
                        return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("Mock gateway never became reachable")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@contextmanager
def mock_gateway():
    """Use this in a `with` block to spin up the mock gateway temporarily."""
    port = find_free_port()
    server = _MockGatewayServer(port)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


# -------------------------------------------------------------------------
# Mock MCP client (replaces ESankhyikiMCPClient methods)
# -------------------------------------------------------------------------


class FakeMCPClient:
    """Drop-in replacement for ESankhyikiMCPClient — no network calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.url = "mock://esankhyiki"
        self.call_count = 0

    async def list_datasets(self) -> dict:
        self.call_count += 1
        return {"datasets": ["PLFS", "CPI", "NSS77", "NSS78", "HCES"]}

    async def get_indicators(self, dataset: str) -> dict:
        self.call_count += 1
        return {"dataset": dataset, "indicators": ["mock_indicator_1", "mock_indicator_2"]}

    async def get_metadata(self, dataset: str, **kwargs: Any) -> dict:
        self.call_count += 1
        return {
            "dataset": dataset,
            "valid_states": ["Bihar", "Uttar Pradesh", "Maharashtra"],
            "valid_years": ["2022-23", "2023-24"],
        }

    async def get_data(self, dataset: str, filters: dict) -> dict:
        self.call_count += 1
        # Plausible mock value depending on the dataset.
        values = {
            "PLFS": {"unemployment_rate": "4.8%", "lfpr": "37.2%"},
            "NSS77": {"small_marginal_share": "91%"},
            "NSS78": {"kachha_dwelling_share": "17%"},
            "CPI": {"inflation_yoy": "5.4%"},
            "HCES": {"poverty_headcount": "33.8%"},
        }
        return {
            "dataset": dataset,
            "filters": filters,
            "value": values.get(dataset, {"mock_value": "demo"}),
        }

    async def quick_fetch(
        self,
        dataset: str,
        state: str | None = None,
        year: str | None = None,
        **extra: Any,
    ) -> tuple[dict, list[dict]]:
        data = await self.get_data(dataset, {"state": state, "year": year, **extra})
        calls = [
            {"tool": "get_metadata", "args": {"dataset": dataset}, "ok": True},
            {"tool": "get_data", "args": {"dataset": dataset, "state": state}, "ok": True},
        ]
        return data, calls
