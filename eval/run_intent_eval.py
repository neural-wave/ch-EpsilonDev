"""Evaluate the intent agent alone: does it flag forbidden requests and leave allowed ones alone?

Cheap and fast (one short call per question), so it is the way to compare intent models and prompts.
    python eval/run_intent_eval.py             # backend and intent model from the environment
    python eval/run_intent_eval.py --shared-only   # only the questions run_eval.py uses
    python eval/run_intent_eval.py --limit 5

Questions: the official examples, the team questions and eval/intent_questions.jsonl (paraphrases,
traps, prompt injection, allowed look-alikes, gray zones). A forbidden question should raise at least
one flag; an allowed one none. Forbidden questions name their rule in the category (e.g. "R5
identifiers"), which also checks that the right flag is raised. The summary breaks results down by group.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from eval_common import load_questions, median, percentile, write_report
from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.guard.decision import FLAG_RULES

RULE_FLAGS = {rule: flag for flag, rule in FLAG_RULES.items()}


async def classify(agent, text: str) -> tuple[list[str], int, bool | None, bool, str | None]:
    """Raised flags, tokens, whether the answer was valid JSON (None without a model answer), whether it
    was cut off at max_tokens, and why the keyword rules answered instead of the model (if they did)."""
    if hasattr(agent, "classify_detailed"):
        reply = await agent.classify_detailed(text)
        return reply.flags.raised(), reply.tokens, reply.valid_json, reply.truncated, reply.fallback
    flags, tokens = await agent.classify(text)
    return flags.raised(), tokens, None, False, None


def by_group(rows: list[dict]) -> dict:
    """Questions, misses and false alarms per question group (official, team, paraphrase, injection, ...)."""
    groups: dict[str, dict] = {}
    for row in rows:
        group = groups.setdefault(row.get("group") or "other", {"questions": 0, "missed": [], "false_alarms": []})
        group["questions"] += 1
        if row["outcome"] == "missed":
            group["missed"].append(row["id"])
        elif row["outcome"] == "false_alarm":
            group["false_alarms"].append(row["id"])
    return groups


async def main_async(args) -> None:
    settings = Settings.from_env()
    agent = build_pipeline(settings).intent_agent
    rows = []
    for item in load_questions(settings, intent_cases=not args.shared_only)[: args.limit or None]:
        started = time.perf_counter()
        try:
            raised, tokens, valid, cut_off, fallback = await classify(agent, item["question"])
            error = None
        except Exception as exc:  # report and keep going
            raised, tokens, valid, cut_off, fallback, error = [], 0, None, False, None, str(exc)[:200]
        latency = (time.perf_counter() - started) * 1000
        should_flag = not item["expected_compliant"]
        outcome = {(True, True): "caught", (True, False): "missed",
                   (False, True): "false_alarm", (False, False): "ok"}[(should_flag, bool(raised))]
        rule = item.get("category", "").split(" ")[0]
        expected_flag = RULE_FLAGS.get(rule)
        rows.append({**item, "raised": raised, "outcome": outcome, "tokens": tokens, "valid_json": valid,
                     "cut_off": cut_off, "fallback": fallback, "latency_ms": round(latency, 1), "error": error,
                     "right_flag": (expected_flag in raised) if expected_flag else None})
        print(f"{outcome:12} {item['id']:12} {latency:6.0f} ms {tokens:5d} tok  {','.join(raised) or '-':40} {item['question'][:60]}")

    forbidden = [r for r in rows if not r["expected_compliant"]]
    allowed = [r for r in rows if r["expected_compliant"]]
    with_rule = [r for r in rows if r["right_flag"] is not None]
    intent = settings.agent("intent")
    summary = {
        "questions": len(rows),
        "backend": settings.backend,
        "model": intent.model if settings.backend == "llm" else "keyword rules (stub)",
        "prompt_version": intent.prompt_version,
        "max_tokens": intent.max_tokens,
        "detection_rate": round(sum(r["outcome"] == "caught" for r in forbidden) / max(len(forbidden), 1), 3),
        "false_alarm_rate": round(sum(r["outcome"] == "false_alarm" for r in allowed) / max(len(allowed), 1), 3),
        "missed": [r["id"] for r in rows if r["outcome"] == "missed"],
        "false_alarms": [r["id"] for r in rows if r["outcome"] == "false_alarm"],
        "right_flag_rate": round(sum(bool(r["right_flag"]) for r in with_rule) / max(len(with_rule), 1), 3),
        "invalid_json": [r["id"] for r in rows if r["valid_json"] is False],
        "cut_off": [r["id"] for r in rows if r["cut_off"]],
        "keyword_fallback": [r["id"] for r in rows if r["fallback"]],
        "errors": [r["id"] for r in rows if r["error"]],
        "median_tokens": median([r["tokens"] for r in rows if not r["error"]]),
        "latency_ms_p50": median([r["latency_ms"] for r in rows if not r["error"]]),
        "latency_ms_p95": round(percentile([r["latency_ms"] for r in rows if not r["error"]], 0.95), 1),
        "by_group": by_group(rows),
    }
    print("\n" + json.dumps(summary, indent=2))
    print(f"report: {write_report(settings, 'intent', summary, rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the intent agent alone.")
    parser.add_argument("--shared-only", action="store_true", help="skip eval/intent_questions.jsonl")
    parser.add_argument("--limit", type=int, default=0)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
