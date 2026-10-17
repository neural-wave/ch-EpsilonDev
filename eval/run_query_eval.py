"""Evaluate the query agent on questions that have a reference SPARQL: does its query return the right result?

    python eval/run_query_eval.py --concurrency 4      # backend from the environment, dev Virtuoso
    python eval/run_query_eval.py --split dev --limit 5
    python eval/run_query_eval.py --rescore eval/reports/query-<date>.json   # execute again, no model calls

Each question runs through the whole pipeline (context, query agent, judge, retry), the way the API
answers it. An official question never sees its own example. Then the generated query and the reference
query both run on VIRTUOSO_ENDPOINT (the dev database, 2,000 patients) and their results are compared:
- "exact": the same rows, whatever the column names or order (numbers rounded to one decimal, labels
  compared lowercased and stripped);
- "exact_numbers": the same numbers in every row, label cells ignored (concept IRIs instead of labels),
  extra generated columns ignored, and a whole number matches the decimal it rounds (ROUND(AVG()) vs
  ROUND(10 * AVG()) / 10); a one-row answer whose columns are the reference's rows (wide instead of
  long) counts too. The strict "exact" stays the headline number; exact_numbers_rate counts both;
- "same_rows": the same number of rows, different values;
- "empty": no rows where the reference has some;
- "wrong": a different number of rows;
- "error": Virtuoso rejected the generated query (execution_error keeps its message) or stopped it at
  its timeout.

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

from eval_common import median, percentile, virtuoso_error_text, write_report
from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.models import PipelineResult
from nl2sparql.virtuoso import VirtuosoClient

OUTCOMES = ("exact", "exact_numbers", "same_rows", "empty", "wrong", "error")
RIGHT = ("exact", "exact_numbers")


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


def _number(value) -> float | None:
    """The cell as a number rounded to one decimal, or None for a label, an IRI or an unbound cell."""
    if value is None or value == "":
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _norm(value) -> str:
    number = _number(value)
    return f"{number:.1f}" if number is not None else str(value).strip().lower()


def signature(rows: list[dict]) -> list[tuple]:
    """The rows' values, independent of column names and order."""
    return sorted(tuple(sorted(_norm(v) for v in row.values())) for row in rows)


def _close(a: float, b: float) -> bool:
    """Equal at one decimal, or one side is a whole number the other rounds to (ROUND(AVG()) vs ROUND(10 * AVG()) / 10)."""
    return a == b or ((a.is_integer() or b.is_integer()) and abs(a - b) <= 0.5)


def _within(needed: list[float], available: list[float]) -> bool:
    """Every needed number has its own close match among the available ones (both lists sorted)."""
    free = list(available)
    for number in needed:
        match = next((i for i, candidate in enumerate(free) if _close(number, candidate)), None)
        if match is None:
            return False
        del free[match]
    return True


def numeric_columns(rows: list[dict]) -> list[str]:
    """Columns whose bound cells are all numbers (at least one): counts, averages, shares, years."""
    columns = sorted({name for row in rows for name in row})
    return [name for name in columns
            if any(_number(row.get(name)) is not None for row in rows)
            and all(_number(row.get(name)) is not None for row in rows if row.get(name) not in (None, ""))]


def numbers(rows: list[dict]) -> list[list[float]]:
    """Per row, its numeric cells sorted; label cells and unbound cells are left out."""
    columns = numeric_columns(rows)
    return [sorted(n for name in columns if (n := _number(row.get(name))) is not None) for row in rows]


def same_numbers(reference_rows: list[dict], generated_rows: list[dict]) -> bool:
    """The generated result carries the reference's numbers: per row, the reference's numeric cells are among
    the generated row's (so an extra generated column is fine, a missing one is not), or one side is a single
    row holding the one number of every row of the other side (wide instead of long)."""
    reference, generated = numbers(reference_rows), numbers(generated_rows)
    if not any(reference) or not any(generated):
        return False
    if len(reference) == len(generated):
        free = list(generated)
        for needed in sorted(reference, key=len, reverse=True):
            match = next((i for i, row in enumerate(free) if _within(needed, row)), None)
            if match is None:
                return False
            del free[match]
        return True
    wide, long = (generated, reference) if len(generated) == 1 else (reference, generated) if len(reference) == 1 else (None, None)
    if wide is None or any(len(row) != 1 for row in long):
        return False
    flat_reference = sorted(n for row in reference for n in row)
    flat_generated = sorted(n for row in generated for n in row)
    if long is generated and len(flat_generated) != len(flat_reference):  # extra columns are tolerated, extra rows are not
        return False
    return _within(flat_reference, flat_generated)


def compare(reference_rows: list[dict], generated_rows: list[dict] | None) -> str:
    if generated_rows is None:
        return "error"
    if signature(reference_rows) == signature(generated_rows):
        return "exact"
    if not generated_rows:
        return "empty"
    if same_numbers(reference_rows, generated_rows):
        return "exact_numbers"
    return "same_rows" if len(generated_rows) == len(reference_rows) else "wrong"


async def execute(client: VirtuosoClient, sparql: str) -> tuple[list[dict] | None, float, str | None]:
    started = time.perf_counter()
    try:
        result = await client.select(sparql)
        return result.rows, round((time.perf_counter() - started) * 1000, 1), None
    except Exception as error:  # report and keep going
        return None, round((time.perf_counter() - started) * 1000, 1), virtuoso_error_text(error)


def rates(rows: list[dict]) -> dict:
    """Aggregate numbers only: safe to show for the holdout split. exact_numbers_rate counts exact results too."""
    n = max(len(rows), 1)
    counts = Counter(r["outcome"] for r in rows)
    return {
        "questions": len(rows),
        "exact_rate": round(counts["exact"] / n, 3),
        "exact_numbers_rate": round(sum(counts[name] for name in RIGHT) / n, 3),
        "executes_rate": round(sum(r["outcome"] != "error" for r in rows) / n, 3),
        "first_try_rate": round(sum(not r["pipeline_error"] and r["retries"] == 0 and not r["judge_errors_final"]
                                    for r in rows) / n, 3),
        "outcomes": {name: counts[name] for name in OUTCOMES},
    }


def generated_row(item: dict, result: PipelineResult, latency_ms: float) -> dict:
    """What the pipeline answered for one question, with the verdict's reason (the first thing one reads when
    an allowed question ends blocked)."""
    return {**item, "pipeline_error": None, "latency_ms": latency_ms,
            "sparql_query": result.sparql_query, "is_compliant": result.is_compliant,
            "compliance_reason": result.compliance_reason, "retries": result.retries,
            "tokens_consumed": result.tokens_consumed, "tokens_by_step": result.tokens_by_step,
            "timings_ms": result.timings_ms, "diagnostics": result.diagnostics}


async def score(rows_in: list[dict], virtuoso: VirtuosoClient) -> list[dict]:
    """Run each generated and reference query on the database and compare their results."""
    rows = []
    for item in rows_in:  # one at a time, so execution times are comparable
        reference_rows, reference_ms, reference_error = await execute(virtuoso, item["reference_sparql"])
        if item["pipeline_error"]:
            generated_rows, generated_ms, generated_error = None, None, item["pipeline_error"]
        else:
            generated_rows, generated_ms, generated_error = await execute(virtuoso, item["sparql_query"])
        diagnostics = item.get("diagnostics") or {}
        row = {**item, "compliance_reason": item.get("compliance_reason"),
               "reference_rows": None if reference_rows is None else len(reference_rows),
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
    return rows


def summarize(rows: list[dict], setup: dict) -> dict:
    scored = [r for r in rows if r["outcome"] != "reference_failed"]
    visible = [r for r in scored if r["split"] != "holdout"]
    ok = [r for r in scored if not r["pipeline_error"]]
    query_tokens = [r["tokens_by_step"].get("query", 0) + r["tokens_by_step"].get("query_retry", 0) for r in ok]
    ratios = [r["execution_ms"] / r["reference_ms"] for r in scored
              if r["outcome"] in (*RIGHT, "same_rows") and r["reference_ms"] and r["execution_ms"]]
    return {
        "questions": len(rows),
        **setup,
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


async def main_async(args) -> None:
    settings = Settings.from_env()
    virtuoso = VirtuosoClient(settings.virtuoso_endpoint, timeout=args.execute_timeout)
    if args.rescore:  # same generated queries, executed again (e.g. after the database was unavailable)
        old = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        keep = ("questions", "outcomes", "exact_rate", "exact_numbers_rate", "executes_rate", "first_try_rate",
                "blocked_allowed", "not_exact", "reference_failed", "retry_reasons", "median_query_tokens",
                "median_tokens", "latency_ms_p50", "latency_ms_p95", "execution_time_ratio_median", "by_split",
                "by_difficulty")
        setup = {key: value for key, value in old["summary"].items() if key not in keep} | {"rescored_from": Path(args.rescore).name}
        rows = await score([r for r in old["rows"] if args.split in ("all", r["split"])], virtuoso)
        summary = summarize(rows, setup)
        print("\n" + json.dumps(summary, indent=2))
        print(f"report: {write_report(settings, 'query', summary, rows)}")
        return

    pipeline = build_pipeline(settings, exclude_exact_examples=True)
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
            return generated_row(item, result, round((time.perf_counter() - started) * 1000, 1))

    rows = await score(await asyncio.gather(*(generate(item) for item in questions)), virtuoso)
    query_agent = settings.agent("query")
    setup = {
        "backend": settings.backend,
        "models": {"intent": settings.agent("intent").model, "query": query_agent.model} if settings.backend == "llm"
                  else {"intent": "stub", "query": "stub"},
        "prompt_versions": {"intent": settings.intent_prompt_version, "query": settings.query_prompt_version},
        "max_tokens": query_agent.max_tokens,
        "context": {"examples": settings.context_examples, "cards": settings.context_cards, "codes": settings.context_codes},
        "query_reorder": settings.query_reorder,
        "concurrency": args.concurrency,
    }
    summary = summarize(rows, setup)
    print("\n" + json.dumps(summary, indent=2))
    print(f"report: {write_report(settings, 'query', summary, rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the query agent against reference SPARQL.")
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--concurrency", type=int, default=1, help="questions generated at once (default 1)")
    parser.add_argument("--execute-timeout", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rescore", metavar="REPORT",
                        help="re-execute the queries of an existing report, or of its --split (no model calls)")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
