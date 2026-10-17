"""Measure concurrent requests to the judged HTTP endpoint without executing SPARQL."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx


DEFAULT_QUESTION = "How many patients have cholera, by sex?"


@dataclass(frozen=True)
class Outcome:
    status: int | None
    elapsed_ms: float
    tokens: int | None
    valid_response: bool


def read_questions(path: Path | None) -> list[str]:
    if path is None:
        return [DEFAULT_QUESTION]
    questions = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        question = item.get("question") if isinstance(item, dict) else None
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"{path}:{number} needs a nonempty 'question' string")
        questions.append(question)
    if not questions:
        raise ValueError(f"{path} contains no questions")
    return questions


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


async def run_load(url: str, questions: list[str], requests: int, concurrency: int,
                   timeout: float, require_llm: bool,
                   transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, list[Outcome], float]:
    base_url = url.rstrip("/")
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, transport=transport) as client:
        health = await client.get(f"{base_url}/health")
        health.raise_for_status()
        health_payload = health.json()
        backend = health_payload.get("backend") if isinstance(health_payload, dict) else None
        if not isinstance(backend, str):
            raise ValueError("/health did not identify the backend")
        if require_llm and backend != "llm":
            raise ValueError(f"expected the llm backend, found {backend!r}")

        semaphore = asyncio.Semaphore(concurrency)

        async def send(index: int) -> Outcome:
            async with semaphore:
                started = time.perf_counter()
                status, tokens, valid = None, None, False
                try:
                    response = await client.post(
                        f"{base_url}/nl-to-sparql",
                        json={"text": questions[index % len(questions)]},
                    )
                    status = response.status_code
                    payload = response.json()
                    valid = (
                        status == 200
                        and isinstance(payload, dict)
                        and isinstance(payload.get("sparql_query"), str)
                        and type(payload.get("tokens_consumed")) is int
                        and isinstance(payload.get("is_compliant"), bool)
                        and (
                            payload["is_compliant"]
                            or (
                                isinstance(payload.get("compliance_reason"), str)
                                and bool(payload["compliance_reason"])
                            )
                        )
                    )
                    tokens = payload.get("tokens_consumed") if valid else None
                except httpx.RequestError:
                    pass
                except ValueError:
                    pass
                return Outcome(status, (time.perf_counter() - started) * 1000, tokens, valid)

        started = time.perf_counter()
        outcomes = await asyncio.gather(*(send(index) for index in range(requests)))
        elapsed = time.perf_counter() - started
    return backend, outcomes, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080", help="Base URL of the API")
    parser.add_argument("--questions", type=Path, help="JSONL file with 'question' fields")
    parser.add_argument("--requests", type=int, default=32, help="Total HTTP requests")
    parser.add_argument("--concurrency", type=int, default=8, help="Maximum simultaneous HTTP requests")
    parser.add_argument("--timeout", type=float, default=120.0, help="Seconds per HTTP request")
    parser.add_argument("--require-llm", action="store_true", help="Reject a stub server")
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.timeout <= 0:
        parser.error("requests, concurrency and timeout must be positive")

    try:
        questions = read_questions(args.questions)
        backend, outcomes, elapsed = asyncio.run(
            run_load(args.url, questions, args.requests, args.concurrency, args.timeout, args.require_llm)
        )
    except (OSError, ValueError, httpx.HTTPError) as error:
        print(f"Load test could not start: {error}", file=sys.stderr)
        return 2

    latencies = [outcome.elapsed_ms for outcome in outcomes]
    statuses = Counter(str(outcome.status) if outcome.status is not None else "network error" for outcome in outcomes)
    failures = sum(not outcome.valid_response for outcome in outcomes)
    tokens = [outcome.tokens for outcome in outcomes if outcome.tokens is not None]
    print(f"backend={backend} requests={len(outcomes)} concurrency={args.concurrency} elapsed_s={elapsed:.2f}")
    print(f"HTTP statuses: {dict(sorted(statuses.items()))}; invalid_or_failed={failures}")
    print(f"latency_ms p50={statistics.median(latencies):.1f} p95={percentile(latencies, 95):.1f}")
    print(f"throughput_rps={len(outcomes) / elapsed:.2f} mean_tokens={statistics.mean(tokens):.0f}" if tokens else
          f"throughput_rps={len(outcomes) / elapsed:.2f} mean_tokens=unavailable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
