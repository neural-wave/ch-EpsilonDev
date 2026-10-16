"""Run the pipeline over the test questions and report verdict accuracy, latency and tokens.

    python eval/run_eval.py                       # in-process, backend from the environment
    python eval/run_eval.py --url http://localhost:8080
    python eval/run_eval.py --execute             # also run compliant queries on VIRTUOSO_ENDPOINT

Official questions come from data/examples.jsonl; team questions from eval/questions.jsonl. By
default the official question's own example is hidden from the context, so it cannot copy it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.virtuoso import VirtuosoClient


def load_questions(settings: Settings) -> list[dict]:
    questions = []
    for line in (settings.data_dir / "examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            questions.append({"id": row["id"], "question": row["question"], "expected_compliant": row["compliant"],
                              "category": "official", "reference_sparql": row["sparql"]})
    team_file = REPO_ROOT / "eval" / "questions.jsonl"
    for line in team_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            questions.append(json.loads(line))
    return questions


async def ask_in_process(pipeline, text: str) -> dict:
    result = await pipeline.run(text)
    return {"sparql_query": result.sparql_query, "tokens_consumed": result.tokens_consumed,
            "is_compliant": result.is_compliant, "compliance_reason": result.compliance_reason,
            "retries": result.retries}


async def ask_over_http(client: httpx.AsyncClient, url: str, text: str) -> dict:
    response = await client.post(f"{url.rstrip('/')}/nl-to-sparql", json={"text": text})
    response.raise_for_status()
    return response.json()


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else 0.0


async def main_async(args) -> None:
    settings = Settings.from_env()
    questions = load_questions(settings)[: args.limit or None]
    pipeline = None if args.url else build_pipeline(settings, exclude_exact_examples=not args.include_exact)
    virtuoso = VirtuosoClient(settings.virtuoso_endpoint, timeout=args.execute_timeout) if args.execute else None
    rows = []
    async with httpx.AsyncClient(timeout=120) as client:
        for item in questions:
            started = time.perf_counter()
            try:
                answer = await (ask_over_http(client, args.url, item["question"]) if args.url
                                else ask_in_process(pipeline, item["question"]))
                error = None
            except Exception as exc:  # report and keep going
                answer, error = {}, str(exc)[:200]
            latency = (time.perf_counter() - started) * 1000
            row = {**item, "latency_ms": round(latency, 1), "error": error, **answer}
            row["correct_verdict"] = answer.get("is_compliant") == item["expected_compliant"]
            if virtuoso and answer.get("is_compliant"):
                try:
                    result = await virtuoso.select(answer["sparql_query"])
                    row.update(executed_rows=len(result.rows), execution_ms=round(result.elapsed_ms, 1))
                except Exception as exc:
                    row["execution_error"] = str(exc)[:200]
            rows.append(row)
            mark = "ok " if row["correct_verdict"] else "XX "
            print(f"{mark}{item['id']:12} expected={item['expected_compliant']!s:5} got={answer.get('is_compliant')!s:5} "
                  f"{latency:7.0f} ms  {item['question'][:70]}")

    latencies = [r["latency_ms"] for r in rows if not r["error"]]
    leaks = [r["id"] for r in rows if r.get("is_compliant") is True and not r["expected_compliant"]]
    blocked = [r["id"] for r in rows if r.get("is_compliant") is False and r["expected_compliant"]]
    summary = {
        "questions": len(rows),
        "verdict_accuracy": round(sum(r["correct_verdict"] for r in rows) / max(len(rows), 1), 3),
        "said_compliant_but_forbidden": leaks,
        "blocked_but_allowed": blocked,
        "errors": [r["id"] for r in rows if r["error"]],
        "latency_ms_p50": round(statistics.median(latencies), 1) if latencies else None,
        "latency_ms_p95": round(percentile(latencies, 0.95), 1) if latencies else None,
        "avg_tokens": round(statistics.mean(r.get("tokens_consumed", 0) for r in rows), 1) if rows else 0,
        "backend": "http" if args.url else settings.backend,
    }
    print("\n" + json.dumps(summary, indent=2))
    out_dir = REPO_ROOT / "eval" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"report: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the NL-to-SPARQL pipeline.")
    parser.add_argument("--url", help="evaluate a running API instead of the in-process pipeline")
    parser.add_argument("--execute", action="store_true", help="run compliant queries on VIRTUOSO_ENDPOINT")
    parser.add_argument("--execute-timeout", type=float, default=120.0)
    parser.add_argument("--include-exact", action="store_true", help="let official questions see their own example")
    parser.add_argument("--limit", type=int, default=0)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
