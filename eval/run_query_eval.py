"""Evaluate the query agent on questions that have a reference SPARQL: does its query return the right result?

    python eval/run_query_eval.py --concurrency 4      # backend from the environment, dev Virtuoso
    python eval/run_query_eval.py --split dev --limit 5

Each question runs through the whole pipeline (context, query agent, judge, retry), the way the API
answers it. An official question never sees its own example. Then the generated query and the reference
query both run on VIRTUOSO_ENDPOINT (the dev database, 2,000 patients) and their results are compared:
- "exact": the same rows, whatever the column names or order (numbers rounded to one decimal);
- "same_rows": the same number of rows, different values;
- "empty": no rows where the reference has some;
- "wrong": a different number of rows;
- "error": Virtuoso rejected the generated query.

Questions: the official compliant examples (data/examples.jsonl) plus eval/query_questions.jsonl.
Holdout questions are a blind exam: they count in by_split only, never in the printed lines or lists.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from eval_common import median, percentile, write_report
from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.virtuoso import VirtuosoClient

OUTCOMES = ("exact", "same_rows", "empty", "wrong", "error")


def load_query_questions(settings: Settings) -> list[dict]:
    questions = []
    for line in (settings.data_dir / "examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["compliant"]:
                questions.append({"id": row["id"], "question": row["question"], "reference_sparql": row["sparql"],
                                  "difficulty": "official", "split": "dev"})
    extra = REPO_ROOT / "eval" / "query_questions.jsonl"
    if extra.exists():
        for line in extra.read_text(encoding="utf-8").splitlines():
            if line.strip():
                questions.append({"split": "dev", **json.loads(line)})
    return questions


def _norm(value) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value).strip()


def signature(rows: list[dict]) -> list[tuple]:
    """The rows' values, independent of column names and order."""
    return sorted(tuple(sorted(_norm(v) for v in row.values())) for row in rows)


def compare(reference_rows: list[dict], generated_rows: list[dict] | None) -> str:
    if generated_rows is None:
        return "error"
    if signature(reference_rows) == signature(generated_rows):
        return "exact"
    if not generated_rows:
        return "empty"
    return "same_rows" if len(generated_rows) == len(reference_rows) else "wrong"


async def execute(client: VirtuosoClient, sparql: str) -> tuple[list[dict] | None, float, str | None]:
    started = time.perf_counter()
    try:
        result = await client.select(sparql)
        return result.rows, round((time.perf_counter() - started) * 1000, 1), None
    except Exception as error:  # report and keep going
        return None, round((time.perf_counter() - started) * 1000, 1), str(error)[:200]


def rates(rows: list[dict]) -> dict:
    """Aggregate numbers only: safe to show for the holdout split."""
    n = max(len(rows), 1)
    counts = Counter(r["outcome"] for r in rows)
    return {
        "questions": len(rows),
        "exact_rate": round(counts["exact"] / n, 3),
        "executes_rate": round(sum(r["outcome"] != "error" for r in rows) / n, 3),
        "first_try_rate": round(sum(not r["pipeline_error"] and r["retries"] == 0 and not r["judge_errors_final"]
                                    for r in rows) / n, 3),
        "outcomes": {name: counts[name] for name in OUTCOMES},
    }


async def main_async(args) -> None:
    settings = Settings.from_env()
    pipeline = build_pipeline(settings, exclude_exact_examples=True)
    virtuoso = VirtuosoClient(settings.virtuoso_endpoint, timeout=args.execute_timeout)
    questions = load_query_questions(settings)
    if args.split != "all":
        questions = [q for q in questions if q["split"] == args.split]
    questions = questions[: args.limit or None]

    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def generate(item: dict) -> dict:
        async with semaphore:
            started = time.perf_counter()
            try:
                result = await pipeline.run(item["question"])
            except Exception as error:  # report and keep going
                return {**item, "pipeline_error": str(error)[:200], "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
            return {**item, "pipeline_error": None, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "sparql_query": result.sparql_query, "is_compliant": result.is_compliant,
                    "retries": result.retries, "tokens_consumed": result.tokens_consumed,
                    "tokens_by_step": result.tokens_by_step, "timings_ms": result.timings_ms,
                    "diagnostics": result.diagnostics}

    generated = await asyncio.gather(*(generate(item) for item in questions))
    rows = []
    for item in generated:  # one at a time, so execution times are comparable
        reference_rows, reference_ms, reference_error = await execute(virtuoso, item["reference_sparql"])
        if item["pipeline_error"]:
            generated_rows, generated_ms, generated_error = None, None, item["pipeline_error"]
        else:
            generated_rows, generated_ms, generated_error = await execute(virtuoso, item["sparql_query"])
        diagnostics = item.get("diagnostics") or {}
        row = {**item, "reference_rows": None if reference_rows is None else len(reference_rows),
               "reference_ms": reference_ms, "reference_error": reference_error,
               "generated_rows": None if generated_rows is None else len(generated_rows),
               "execution_ms": generated_ms, "execution_error": generated_error,
               "outcome": "reference_failed" if reference_rows is None else compare(reference_rows, generated_rows),
               "retries": item.get("retries", 0), "judge_errors_final": diagnostics.get("judge_errors_final", [])}
        rows.append(row)
        if row["split"] != "holdout":
            print(f"{row['outcome']:16} {row['id']:12} retries={row['retries']} "
                  f"rows {row['generated_rows']} vs {row['reference_rows']}  {row['question'][:70]}")
    print(f"{sum(r['split'] == 'holdout' for r in rows)} holdout questions answered (hidden)")

    scored = [r for r in rows if r["outcome"] != "reference_failed"]
    visible = [r for r in scored if r["split"] != "holdout"]
    ok = [r for r in scored if not r["pipeline_error"]]
    query_tokens = [r["tokens_by_step"].get("query", 0) + r["tokens_by_step"].get("query_retry", 0) for r in ok]
    ratios = [r["execution_ms"] / r["reference_ms"] for r in scored
              if r["outcome"] in ("exact", "same_rows") and r["reference_ms"] and r["execution_ms"]]
    query_agent = settings.agent("query")
    summary = {
        "questions": len(rows),
        "backend": settings.backend,
        "models": {"intent": settings.agent("intent").model, "query": query_agent.model} if settings.backend == "llm"
                  else {"intent": "stub", "query": "stub"},
        "prompt_versions": {"intent": settings.intent_prompt_version, "query": settings.query_prompt_version},
        "max_tokens": query_agent.max_tokens,
        "concurrency": args.concurrency,
        **rates(scored),
        "blocked_allowed": [r["id"] for r in visible if r.get("is_compliant") is False],
        "not_exact": [r["id"] for r in visible if r["outcome"] != "exact"],
        "reference_failed": [r["id"] for r in rows if r["outcome"] == "reference_failed" and r["split"] != "holdout"],
        "retry_reasons": dict(Counter((r.get("diagnostics") or {}).get("retry_reason", "none") for r in ok)),
        "median_query_tokens": median(query_tokens),
        "median_tokens": median([r["tokens_consumed"] for r in ok]),
        "latency_ms_p50": median([r["latency_ms"] for r in ok]),
        "latency_ms_p95": round(percentile([r["latency_ms"] for r in ok], 0.95), 1) if ok else None,
        "execution_time_ratio_median": round(statistics.median(ratios), 2) if ratios else None,
        "by_split": {split: rates([r for r in scored if r["split"] == split])
                     for split in ("dev", "holdout") if any(r["split"] == split for r in scored)},
        "by_difficulty": {level: rates([r for r in visible if r.get("difficulty") == level])
                          for level in sorted({r.get("difficulty", "") for r in visible})},
    }
    print("\n" + json.dumps(summary, indent=2))
    print(f"report: {write_report(settings, 'query', summary, rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the query agent against reference SPARQL.")
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--concurrency", type=int, default=1, help="questions generated at once (default 1)")
    parser.add_argument("--execute-timeout", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=0)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
