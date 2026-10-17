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


class VirtuosoTimeout(RuntimeError):
    """Virtuoso stopped the query at its anytime timeout and answered with the rows it had so far.

    The answer is HTTP 206 with the header X-SQL-State S1TAT; its aggregates are wrong, so a caller
    must never take it for a complete result.
    """


def _complete(response: httpx.Response) -> httpx.Response:
    """The response, unless it is an HTTP error or a partial result."""
    response.raise_for_status()
    if response.status_code == 206 or response.headers.get("X-SQL-State") == "S1TAT":
        took = response.headers.get("X-Exec-Milliseconds")
        raise VirtuosoTimeout("Virtuoso stopped the query at its timeout and returned a partial result "
                              f"(HTTP {response.status_code}, X-SQL-State S1TAT{f', {took} ms' if took else ''})")
    return response


def _parse(payload: dict, elapsed_ms: float) -> SelectResult:
    columns = payload.get("head", {}).get("vars", [])
    bindings = payload.get("results", {}).get("bindings", [])
    rows = [{name: cell.get("value", "") for name, cell in binding.items()} for binding in bindings]
    return SelectResult(columns, rows, elapsed_ms)


def select(endpoint: str, query: str, timeout: float = 900.0) -> SelectResult:
    """Run a SELECT query synchronously (for scripts)."""
    started = time.perf_counter()
    response = httpx.post(endpoint, data={"query": query}, headers={"Accept": ACCEPT_JSON}, timeout=timeout)
    return _parse(_complete(response).json(), (time.perf_counter() - started) * 1000)


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
        return _parse(_complete(response).json(), (time.perf_counter() - started) * 1000)
