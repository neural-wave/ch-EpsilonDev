"""Minimal client for a Virtuoso SPARQL endpoint.

Used by the demo page, the eval harness and the data scripts. The /nl-to-sparql endpoint never
calls it: the API only translates and judges, it does not execute queries.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

ACCEPT_JSON = "application/sparql-results+json"


@dataclass(frozen=True)
class SelectResult:
    columns: list[str]
    rows: list[dict[str, str]]
    elapsed_ms: float


def _parse(payload: dict, elapsed_ms: float) -> SelectResult:
    columns = payload.get("head", {}).get("vars", [])
    bindings = payload.get("results", {}).get("bindings", [])
    rows = [{name: cell.get("value", "") for name, cell in binding.items()} for binding in bindings]
    return SelectResult(columns, rows, elapsed_ms)


def select(endpoint: str, query: str, timeout: float = 900.0) -> SelectResult:
    """Run a SELECT query synchronously (for scripts)."""
    started = time.perf_counter()
    response = httpx.post(endpoint, data={"query": query}, headers={"Accept": ACCEPT_JSON}, timeout=timeout)
    response.raise_for_status()
    return _parse(response.json(), (time.perf_counter() - started) * 1000)


class VirtuosoClient:
    """Async client (for the demo and the eval harness)."""

    def __init__(self, endpoint: str, timeout: float = 120.0, transport: httpx.AsyncBaseTransport | None = None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.transport = transport

    async def select(self, query: str) -> SelectResult:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(self.endpoint, data={"query": query}, headers={"Accept": ACCEPT_JSON})
        response.raise_for_status()
        return _parse(response.json(), (time.perf_counter() - started) * 1000)
