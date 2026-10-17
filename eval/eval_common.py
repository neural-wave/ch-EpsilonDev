"""Shared by the eval scripts: the labeled questions and small statistics helpers."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from nl2sparql.config import REPO_ROOT, Settings


def load_questions(settings: Settings, intent_cases: bool = False) -> list[dict]:
    """Official examples (data/examples.jsonl) plus team questions (eval/questions.jsonl).

    intent_cases adds eval/intent_questions.jsonl: extra cases for the intent agent alone
    (paraphrases, traps, prompt injection, allowed look-alikes, gray zones). They have no reference SPARQL.
    """
    questions = []
    for line in (settings.data_dir / "examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            questions.append({"id": row["id"], "question": row["question"], "expected_compliant": row["compliant"],
                              "category": "official", "group": "official", "split": "dev",
                              "reference_sparql": row["sparql"]})
    files = [("questions.jsonl", "team")] + ([("intent_questions.jsonl", "intent")] if intent_cases else [])
    for name, default_group in files:
        for line in (REPO_ROOT / "eval" / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                questions.append({"group": default_group, "split": "dev", **json.loads(line)})
    return questions


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else 0.0


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def write_report(settings: Settings, name: str, summary: dict, rows: list[dict]) -> Path:
    """Saves eval/reports/<name>-<date>-<time>.json; the agents console (/agents) lists these files."""
    from datetime import datetime

    out_dir = settings.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    return out
