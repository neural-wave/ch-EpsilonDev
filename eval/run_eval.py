"""Run the pipeline over the test questions and report verdict accuracy, latency and tokens.

    python eval/run_eval.py                       # in-process, backend from the environment
    python eval/run_eval.py --url http://localhost:8080
    python eval/run_eval.py --execute             # also run compliant queries on VIRTUOSO_ENDPOINT

Official questions come from data/examples.jsonl; team questions from eval/questions.jsonl. By
default the official question's own example is hidden from the context, so it cannot copy it.
In-process runs also report tokens and time per agent, and why retries happened.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter

import httpx

from eval_common import load_questions, median, percentile, write_report
from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.virtuoso import VirtuosoClient


async def ask_in_process(pipeline, text: str) -> dict:
    result = await pipeline.run(text)
    return {"sparql_query": result.sparql_query, "tokens_consumed": result.tokens_consumed,
            "is_compliant": result.is_compliant, "compliance_reason": result.compliance_reason,
            "retries": result.retries, "tokens_by_step": result.tokens_by_step,
            "timings_ms": result.timings_ms, "diagnostics": result.diagnostics}


async def ask_over_http(client: httpx.AsyncClient, url: str, text: str) -> dict:
    response = await client.post(f"{url.rstrip('/')}/nl-to-sparql", json={"text": text})
    response.raise_for_status()
    return response.json()


def per_agent_summary(rows: list[dict]) -> dict:
    """Tokens and time per agent, and why retries happened (in-process runs only)."""
    measured = [r for r in rows if r.get("tokens_by_step") is not None and not r["error"]]
    if not measured:
        return {}
    first_judge_errors = Counter(
        error.split(":")[0][:60] for r in measured for error in r["diagnostics"].get("judge_errors_first_try", [])
    )
    return {
        "median_tokens": {step: median([r["tokens_by_step"].get(step, 0) for r in measured if step in r["tokens_by_step"]])
                          for step in ("intent", "query", "query_retry")},
        "median_time_ms": {step: median([r["timings_ms"][step] for r in measured if step in r["timings_ms"]])
                           for step in ("intent", "generate", "generate_retry", "guard")},
        "retry_reasons": dict(Counter(r["diagnostics"].get("retry_reason", "none") for r in measured)),
        "first_try_judge_errors": dict(first_judge_errors.most_common(5)),
        "still_invalid_after_retry": [r["id"] for r in measured if r["diagnostics"].get("judge_errors_final")],
        "cut_off": dict(Counter(step for r in measured for step in r["diagnostics"].get("cut_off", []))),
        "intent_invalid_json": [r["id"] for r in measured if r["diagnostics"].get("intent_invalid_json")],
    }


def agent_setup(settings: Settings) -> dict:
    agents = {name: settings.agent(name) for name in ("intent", "query")}
    llm = settings.backend == "llm"
    return {
        "models": {name: agent.model if llm else "stub" for name, agent in agents.items()},
        "prompt_versions": {name: agent.prompt_version for name, agent in agents.items()},
        "max_tokens": {name: agent.max_tokens for name, agent in agents.items()},
    }


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
    summary = {
        "questions": len(rows),
        "verdict_accuracy": round(sum(r["correct_verdict"] for r in rows) / max(len(rows), 1), 3),
        "said_compliant_but_forbidden": [r["id"] for r in rows if r.get("is_compliant") is True and not r["expected_compliant"]],
        "blocked_but_allowed": [r["id"] for r in rows if r.get("is_compliant") is False and r["expected_compliant"]],
        "errors": [r["id"] for r in rows if r["error"]],
        "latency_ms_p50": round(statistics.median(latencies), 1) if latencies else None,
        "latency_ms_p95": round(percentile(latencies, 0.95), 1) if latencies else None,
        "avg_tokens": round(statistics.mean(r.get("tokens_consumed", 0) for r in rows), 1) if rows else 0,
        "backend": "http" if args.url else settings.backend,
        # Over --url the remote server's configuration is unknown, so it is not reported.
        **({} if args.url else agent_setup(settings)),
        "per_agent": per_agent_summary(rows),
    }
    if virtuoso:
        executed = [r for r in rows if r.get("is_compliant")]
        summary["execution"] = {
            "compliant_queries": len(executed),
            "returned_rows": sum(1 for r in executed if r.get("executed_rows")),
            "returned_nothing": [r["id"] for r in executed if r.get("executed_rows") == 0],
            "failed_in_virtuoso": [r["id"] for r in executed if r.get("execution_error")],
        }
    print("\n" + json.dumps(summary, indent=2))
    print(f"report: {write_report(settings, 'pipeline', summary, rows)}")


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
