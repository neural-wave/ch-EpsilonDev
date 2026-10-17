"""One full run: every eval for the current configuration, labeled with what changed.

    python eval/run_all.py --label "query prompt v6: no DEFINE, inference only on concrete codes"
    python eval/run_all.py --label "..." --skip-perf        # when the perf database is busy

Runs, in this order: the intent agent (198 questions), the query agent against reference SPARQL (87),
the whole pipeline on the 198 labeled questions (the verdict metric), and the timing of the generated
queries on the 100,000-patient database. Each report is written to eval/reports/ and the label is
saved next to them in eval/reports/notes.json, which the runs page (http://localhost:8080/agents/runs)
shows as the run's note. The label should say what was changed, so the comparison reads as an
experiment: one change, one run, one verdict.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from nl2sparql.config import REPO_ROOT, Settings

EVAL = REPO_ROOT / "eval"
REPORT_RE = re.compile(r"^report: (\S+)$", re.MULTILINE)
SUMMARY_PREFIXES = ("ok ", "XX ", "exact", "same", "empty", "wrong", "error", "caught", "missed", "false_alarm", "holdout")


def tail_matching(stdout: str, n: int = 6) -> str:
    """The last n lines of an eval script's output that are per-question or summary lines."""
    lines = [line for line in stdout.splitlines(keepends=True) if line.startswith(SUMMARY_PREFIXES)]
    return "".join(lines[-n:])


def run(script: str, args: list[str], label: str) -> Path | None:
    started = time.perf_counter()
    print(f"== {script} {' '.join(args)}", flush=True)
    result = subprocess.run([sys.executable, str(EVAL / script), *args], capture_output=True, text=True)
    sys.stdout.write(tail_matching(result.stdout))
    match = REPORT_RE.search(result.stdout)
    if result.returncode != 0 or not match:
        print(result.stderr[-2000:] or result.stdout[-2000:])
        print(f"   FAILED after {time.perf_counter() - started:.0f} s", flush=True)
        return None
    path = Path(match.group(1))
    save_note(path.name, label)
    print(f"   {path.name} in {time.perf_counter() - started:.0f} s", flush=True)
    return path


def save_note(report_name: str, label: str) -> None:
    notes_file = Settings.from_env().reports_dir / "notes.json"
    notes = json.loads(notes_file.read_text(encoding="utf-8")) if notes_file.exists() else {}
    notes[report_name] = label
    notes_file.write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")


def summarize(path: Path | None, kind: str) -> str:
    if path is None:
        return f"{kind}: failed"
    s = json.loads(path.read_text(encoding="utf-8"))["summary"]
    if kind == "intent":
        h = s["by_split"].get("holdout", {})
        return f"intent: holdout detected {h.get('detection_rate', 0):.1%}, false alarms {h.get('false_alarm_rate', 0):.1%}, {s['median_tokens']} tokens"
    if kind == "query":
        h = s["by_split"].get("holdout", {})
        return (f"query: exact {s['outcomes']['exact']}/{s['questions']}, runs {s['executes_rate']:.0%} (holdout {h.get('executes_rate', 0):.0%}), "
                f"first try {s['first_try_rate']:.0%}, blocked {len(s['blocked_allowed'])}, {s['median_query_tokens']} query tokens, p50 {s['latency_ms_p50'] / 1000:.1f} s")
    if kind == "pipeline":
        h, d = s["by_split"].get("holdout", {}), s["by_split"].get("dev", {})
        return (f"pipeline: verdicts dev {d.get('verdict_accuracy', 0):.1%} / holdout {h.get('verdict_accuracy', 0):.1%}, "
                f"holdout leaks {h.get('forbidden_marked_compliant')} blocked {h.get('allowed_blocked')}, p50 {s['latency_ms_p50'] / 1000:.1f} s, {s['avg_tokens']:.0f} tokens")
    return (f"perf: {s['both_complete']}/{s['queries']} pairs complete, median generated {s['median_generated_seconds']} s vs reference "
            f"{s['median_reference_seconds']} s, generated timeouts {len(s['generated_timed_out'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run every eval for the current configuration and label the run.")
    parser.add_argument("--label", required=True, help="what this run tests, e.g. 'query prompt v6'")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--skip-intent", action="store_true", help="the change cannot affect the intent agent")
    parser.add_argument("--skip-perf", action="store_true")
    parser.add_argument("--perf-runs", type=int, default=2)
    args = parser.parse_args()
    conc = ["--concurrency", str(args.concurrency)]
    started = time.perf_counter()
    intent = None if args.skip_intent else run("run_intent_eval.py", conc, args.label)
    query = run("run_query_eval.py", [*conc, "--execute-timeout", "30"], args.label)
    pipeline = run("run_eval.py", ["--intent-cases", *conc], args.label)
    perf = None
    if query and not args.skip_perf:
        perf = run("time_on_perf.py", [str(query), "--runs", str(args.perf_runs), "--timeout-ms", "60000"], args.label)
    skipped = {"intent": args.skip_intent, "perf": args.skip_perf}
    print(f"\n== {args.label} ({(time.perf_counter() - started) / 60:.0f} min)")
    for path, kind in ((intent, "intent"), (query, "query"), (pipeline, "pipeline"), (perf, "perf")):
        if not skipped.get(kind):
            print("   " + summarize(path, kind))
    print("   runs page: http://localhost:8080/agents/runs")


if __name__ == "__main__":
    main()
