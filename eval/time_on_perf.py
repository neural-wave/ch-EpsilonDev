"""Time generated queries against their reference queries on the perf database (100,000 patients).

    python eval/time_on_perf.py eval/reports/query-<date>.json --limit 5 --runs 1     # pilot
    python eval/time_on_perf.py eval/reports/query-<date>.json --runs 3 --timeout-ms 60000

Takes the queries of a query-eval report (only the generated ones that ran on dev) and executes each
generated query and its reference on VIRTUOSO_PERF_ENDPOINT (default http://localhost:8891/sparql).
Virtuoso's own `timeout` parameter stops a query on the server at the deadline and returns partial
results, so a slow query cannot saturate the database the way a client timeout does. The first run of
each query warms the cache and is not timed; the median of the timed runs is reported.

Query execution time on the organizers' machine is a main scoring criterion. This harness is for us
only: nothing here is reachable from the API.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import httpx

from eval_common import median, write_report
from nl2sparql.config import Settings

ACCEPT_JSON = "application/sparql-results+json"


def run_query(endpoint: str, sparql: str, timeout_ms: int) -> dict:
    """One execution. Virtuoso answers partial results with X-SQL-State S1TAT when the timeout hits."""
    started = time.perf_counter()
    try:
        response = httpx.post(endpoint, data={"query": sparql, "timeout": str(timeout_ms)},
                              headers={"Accept": ACCEPT_JSON}, timeout=timeout_ms / 1000 + 60)
        seconds = round(time.perf_counter() - started, 2)
        # HTTP 206 is Virtuoso's "partial content": the server-side timeout stopped the query.
        if response.status_code not in (200, 206):
            return {"seconds": seconds, "rows": None, "complete": False, "error": response.text.strip()[:160]}
        rows = len(response.json().get("results", {}).get("bindings", []))
        complete = response.status_code == 200 and response.headers.get("X-SQL-State", "") != "S1TAT"
        return {"seconds": seconds, "rows": rows, "complete": complete, "error": None}
    except Exception as error:  # report and keep going
        return {"seconds": round(time.perf_counter() - started, 2), "rows": None, "complete": False, "error": str(error)[:160]}


def time_query(endpoint: str, sparql: str, runs: int, timeout_ms: int) -> dict:
    first = run_query(endpoint, sparql, timeout_ms)  # warm-up
    if first["error"] or not first["complete"]:
        return {**first, "runs": 0, "warm_up_seconds": first["seconds"]}
    timed = [run_query(endpoint, sparql, timeout_ms) for _ in range(runs)]
    good = [r["seconds"] for r in timed if not r["error"] and r["complete"]]
    return {"seconds": round(statistics.median(good), 2) if good else None, "rows": first["rows"],
            "complete": len(good) == runs, "error": next((r["error"] for r in timed if r["error"]), None),
            "runs": len(good), "warm_up_seconds": first["seconds"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Time generated queries against references on the perf database.")
    parser.add_argument("report", help="a query-eval report (eval/reports/query-*.json)")
    parser.add_argument("--endpoint", default=os.environ.get("VIRTUOSO_PERF_ENDPOINT", "http://localhost:8891/sparql"))
    parser.add_argument("--runs", type=int, default=2, help="timed runs after one warm-up (default 2)")
    parser.add_argument("--timeout-ms", type=int, default=60000, help="server-side limit per execution")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    candidates = [r for r in report["rows"] if r.get("sparql_query") and r.get("generated_rows") is not None]
    candidates = candidates[: args.limit or None]
    print(f"{len(candidates)} queries ran on dev; timing them on {args.endpoint} "
          f"(1 warm-up + {args.runs} timed runs, {args.timeout_ms} ms server limit)")
    rows = []
    for item in candidates:
        reference = time_query(args.endpoint, item["reference_sparql"], args.runs, args.timeout_ms)
        generated = time_query(args.endpoint, item["sparql_query"], args.runs, args.timeout_ms)
        both = reference["complete"] and generated["complete"] and reference["seconds"] and generated["seconds"] is not None
        ratio = round(generated["seconds"] / reference["seconds"], 2) if both else None
        rows.append({"id": item["id"], "split": item.get("split", "dev"), "question": item["question"],
                     "dev_outcome": item.get("outcome"), "reference": reference, "generated": generated, "ratio": ratio})
        fmt = lambda r: ("error" if r["error"] else "timeout" if not r["complete"] else f"{r['seconds']:.2f}s")
        print(f"{item['id']:12} reference {fmt(reference):>8}  generated {fmt(generated):>8}  "
              f"ratio {ratio if ratio is not None else '-':>6}  {item['question'][:55]}")

    complete = [r for r in rows if r["reference"]["complete"] and r["generated"]["complete"]]
    summary = {
        "queries": len(rows),
        "endpoint": args.endpoint,
        "runs": args.runs,
        "timeout_ms": args.timeout_ms,
        "source_report": Path(args.report).name,
        "both_complete": len(complete),
        "reference_timed_out": [r["id"] for r in rows if not r["reference"]["complete"] and not r["reference"]["error"]],
        "generated_timed_out": [r["id"] for r in rows if not r["generated"]["complete"] and not r["generated"]["error"]],
        "generated_errors": [r["id"] for r in rows if r["generated"]["error"]],
        "median_reference_seconds": median([r["reference"]["seconds"] for r in complete]),
        "median_generated_seconds": median([r["generated"]["seconds"] for r in complete]),
        "median_ratio_generated_over_reference": median([r["ratio"] for r in complete if r["ratio"] is not None]),
        "slower_than_reference": [r["id"] for r in complete if r["ratio"] and r["ratio"] > 2],
    }
    print("\n" + json.dumps(summary, indent=2))
    print(f"report: {write_report(Settings.from_env(), 'perf', summary, rows)}")


if __name__ == "__main__":
    main()
