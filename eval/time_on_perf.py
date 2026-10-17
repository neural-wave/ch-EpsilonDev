"""Time generated queries against their reference queries on the perf database (100,000 patients).

    python eval/time_on_perf.py eval/reports/query-<date>.json --limit 5 --runs 1     # pilot
    python eval/time_on_perf.py eval/reports/query-<date>.json --runs 3 --timeout-ms 60000

Takes the queries of a query-eval report (only the generated ones that ran on dev) and executes each
generated query and its reference on VIRTUOSO_PERF_ENDPOINT (default http://localhost:8891/sparql),
one at a time, as plain runs: no anytime `timeout` parameter, because it changes the plan of
OPTIONAL + inference queries, which then run to the deadline and return partial rows (see
docs/FAILURE_ANALYSIS.md §3.6). --timeout-ms is a client-side deadline instead. A disconnect does not
stop a Virtuoso query, so a query that overruns is marked timed out and the perf container (found with
`docker ps` by the endpoint's port) is restarted before the next query, waiting until the endpoint
answers a trivial query again. The first run of each query warms the cache and is not timed; the
median of the timed runs is reported.

Query execution time on the organizers' machine is a main scoring criterion. This harness is for us
only: nothing here is reachable from the API.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from eval_common import median, virtuoso_error_text, write_report
from nl2sparql.config import Settings
from nl2sparql.virtuoso import VirtuosoTimeout, select

TRIVIAL_QUERY = "SELECT * WHERE { ?s ?p ?o } LIMIT 1"


def find_container(endpoint: str) -> str:
    """The running docker container that publishes the endpoint's port (virtuoso-perf for :8891)."""
    port = urlparse(endpoint).port or 80
    listing = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
                             capture_output=True, text=True, check=True).stdout
    for line in listing.splitlines():
        name, _, ports = line.partition("\t")
        if f":{port}->" in ports:
            return name
    raise RuntimeError(f"no running docker container publishes port {port}: cannot restart Virtuoso after an overrun")


class PerfClient:
    """Plain runs on the perf endpoint, and the container restart that stops an overrunning query."""

    def __init__(self, endpoint: str, deadline_s: float, restart_wait_s: float = 600.0):
        self.endpoint = endpoint
        self.deadline_s = deadline_s
        self.restart_wait_s = restart_wait_s
        self.container = None

    def run(self, sparql: str) -> dict:
        """One execution: seconds, rows, whether it completed, Virtuoso's error. An overrun has no error text;
        a partial result (Virtuoso's own limit stopped the query) is an overrun that needs no restart."""
        started = time.perf_counter()
        try:
            result = select(self.endpoint, sparql, timeout=self.deadline_s)
            return {"seconds": round(result.elapsed_ms / 1000, 2), "rows": len(result.rows), "complete": True, "error": None}
        except httpx.TimeoutException:
            return {"seconds": round(time.perf_counter() - started, 2), "rows": None, "complete": False, "error": None}
        except VirtuosoTimeout:
            return {"seconds": round(time.perf_counter() - started, 2), "rows": None, "complete": False, "error": None,
                    "stopped_by_server": True}
        except Exception as error:  # report and keep going
            return {"seconds": round(time.perf_counter() - started, 2), "rows": None, "complete": False,
                    "error": virtuoso_error_text(error, 160)}

    def restart(self) -> None:
        """Restart the container and wait until the endpoint answers again (the data reloads from disk)."""
        self.container = self.container or find_container(self.endpoint)
        print(f"  restarting {self.container} (a disconnect does not stop a Virtuoso query)")
        subprocess.run(["docker", "restart", self.container], capture_output=True, text=True, check=True)
        deadline = time.monotonic() + self.restart_wait_s
        while True:
            try:
                select(self.endpoint, TRIVIAL_QUERY, timeout=10)
                return
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{self.endpoint} did not answer within {self.restart_wait_s:.0f} s of restarting "
                                       f"{self.container}")
                time.sleep(2)


def overran(result: dict) -> bool:
    return not result["complete"] and not result["error"]


def still_running(result: dict) -> bool:
    """An overrun past the client deadline: the query keeps running in Virtuoso until the container restarts."""
    return overran(result) and not result.get("stopped_by_server")


def time_query(client, sparql: str, runs: int) -> dict:
    """One warm-up and `runs` timed runs, stopping at the first overrun or error (the caller restarts Virtuoso)."""
    first = client.run(sparql)
    if first["error"] or not first["complete"]:
        return {**first, "runs": 0, "warm_up_seconds": first["seconds"]}
    timed = []
    for _ in range(runs):
        timed.append(client.run(sparql))
        if timed[-1]["error"] or not timed[-1]["complete"]:
            break
    good = [r["seconds"] for r in timed if not r["error"] and r["complete"]]
    result = {"seconds": round(statistics.median(good), 2) if good else None, "rows": first["rows"],
              "complete": len(good) == runs, "error": next((r["error"] for r in timed if r["error"]), None),
              "runs": len(good), "warm_up_seconds": first["seconds"]}
    if any(r.get("stopped_by_server") for r in timed):
        result["stopped_by_server"] = True
    return result


def known_reference_times(paths: list[str], reports_dir: Path) -> dict[str, tuple[str, dict]]:
    """Reference timings from earlier perf reports, by question id, with the reference query they timed
    (read from each perf report's source query report), so a reference that changed since is timed again.
    The reference queries are the same from run to run and the slow ones cost a container restart each:
    reusing them is what keeps a perf run under half an hour. Earlier paths win on duplicates."""
    known = {}
    for path in paths:
        perf = json.loads(Path(path).read_text(encoding="utf-8"))
        source = reports_dir / perf["summary"].get("source_report", "")
        if not source.exists():
            continue
        texts = {r["id"]: r.get("reference_sparql") for r in json.loads(source.read_text(encoding="utf-8"))["rows"]}
        for row in perf["rows"]:
            if row["id"] not in known and texts.get(row["id"]):
                known[row["id"]] = (texts[row["id"]], {**row["reference"], "reused_from": Path(path).name})
    return known


def time_pairs(candidates: list[dict], client, runs: int, known: dict | None = None) -> tuple[list[dict], int]:
    """Reference then generated query of each candidate; the number of container restarts comes second.
    A reference whose timing is known from an earlier report (same query text) is not run again."""
    rows, restarts = [], 0
    known = known or {}
    for item in candidates:
        timed = {}
        for side, sparql in (("reference", item["reference_sparql"]), ("generated", item["sparql_query"])):
            if side == "reference" and item["id"] in known and known[item["id"]][0] == sparql:
                timed[side] = known[item["id"]][1]
                continue
            timed[side] = time_query(client, sparql, runs)
            if still_running(timed[side]):
                client.restart()
                restarts += 1
        reference, generated = timed["reference"], timed["generated"]
        both = reference["complete"] and generated["complete"] and reference["seconds"] and generated["seconds"] is not None
        ratio = round(generated["seconds"] / reference["seconds"], 2) if both else None
        rows.append({"id": item["id"], "split": item.get("split", "dev"), "question": item["question"],
                     "dev_outcome": item.get("outcome"), "reference": reference, "generated": generated, "ratio": ratio})
        fmt = lambda r: ("error" if r["error"] else "timeout" if not r["complete"] else f"{r['seconds']:.2f}s")
        print(f"{item['id']:12} reference {fmt(reference):>8}  generated {fmt(generated):>8}  "
              f"ratio {ratio if ratio is not None else '-':>6}  {item['question'][:55]}")
    return rows, restarts


def summarize(rows: list[dict], setup: dict) -> dict:
    complete = [r for r in rows if r["reference"]["complete"] and r["generated"]["complete"]]
    return {
        "queries": len(rows),
        **setup,
        "both_complete": len(complete),
        "reference_timed_out": [r["id"] for r in rows if overran(r["reference"])],
        "generated_timed_out": [r["id"] for r in rows if overran(r["generated"])],
        "generated_errors": [r["id"] for r in rows if r["generated"]["error"]],
        "median_reference_seconds": median([r["reference"]["seconds"] for r in complete]),
        "median_generated_seconds": median([r["generated"]["seconds"] for r in complete]),
        "median_ratio_generated_over_reference": median([r["ratio"] for r in complete if r["ratio"] is not None]),
        "slower_than_reference": [r["id"] for r in complete if r["ratio"] and r["ratio"] > 2],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Time generated queries against references on the perf database.")
    parser.add_argument("report", help="a query-eval report (eval/reports/query-*.json)")
    parser.add_argument("--endpoint", default=os.environ.get("VIRTUOSO_PERF_ENDPOINT", "http://localhost:8891/sparql"))
    parser.add_argument("--runs", type=int, default=2, help="timed runs after one warm-up (default 2)")
    parser.add_argument("--timeout-ms", type=int, default=60000,
                        help="client-side deadline per execution; past it the query is marked timed out and Virtuoso restarted")
    parser.add_argument("--restart-wait", type=float, default=600.0, help="seconds to wait for Virtuoso after a restart")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--reference-times", nargs="*", default=[], metavar="PERF_REPORT",
                        help="earlier perf reports whose reference timings are reused when the reference query is unchanged")
    args = parser.parse_args()

    settings = Settings.from_env()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    candidates = [r for r in report["rows"] if r.get("sparql_query") and r.get("generated_rows") is not None]
    candidates = candidates[: args.limit or None]
    known = known_reference_times(args.reference_times, settings.reports_dir)
    reused = sum(1 for r in candidates if r["id"] in known and known[r["id"]][0] == r["reference_sparql"])
    print(f"{len(candidates)} queries ran on dev; timing them on {args.endpoint} "
          f"(1 warm-up + {args.runs} timed runs, {args.timeout_ms} ms deadline, plain runs; "
          f"{reused} reference timings reused)")
    client = PerfClient(args.endpoint, args.timeout_ms / 1000, args.restart_wait)
    rows, restarts = time_pairs(candidates, client, args.runs, known)
    summary = summarize(rows, {"endpoint": args.endpoint, "runs": args.runs, "timeout_ms": args.timeout_ms,
                               "source_report": Path(args.report).name, "restarts": restarts,
                               "reference_timings_reused": reused})
    print("\n" + json.dumps(summary, indent=2))
    print(f"report: {write_report(settings, 'perf', summary, rows)}")


if __name__ == "__main__":
    main()
