"""Export the queries the LLM really wrote into data/guard_corpus.jsonl, for guard work without a database.

Reads the request log of the run machine (logs/requests.jsonl), keeps the LLM requests, labels them
with the expected verdict of the question, and stores the guard's current opinion of each query.
The file holds no patient data and no terminology content (only queries and code IRIs).
    python scripts/export_guard_corpus.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nl2sparql.config import REPO_ROOT
from nl2sparql.guard import ComplianceGuard, PropertyKinds


def labels() -> dict[str, tuple[str, bool]]:
    found = {}
    for line in (REPO_ROOT / "data" / "examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            found[row["question"].strip()] = (row["id"], row["compliant"])
    for line in (REPO_ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            found[row["question"].strip()] = (row["id"], row["expected_compliant"])
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, default=REPO_ROOT / "logs" / "requests.jsonl")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "guard_corpus.jsonl")
    args = parser.parse_args()
    guard = ComplianceGuard(PropertyKinds.load(REPO_ROOT / "data" / "property_kinds.json"))
    known = labels()
    seen, rows = set(), []
    for line in args.log.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["tokens_consumed"] == 0 or record["text"].strip() not in known:
            continue  # stand-in runs copy examples; unlabeled questions cannot be judged
        key = (record["text"], record["sparql_query"])
        if key in seen:
            continue
        seen.add(key)
        question_id, expected = known[record["text"].strip()]
        rows.append({
            "question_id": question_id,
            "question": record["text"],
            "expected_compliant": expected,
            "sparql": record["sparql_query"],
            "intent_flags": [name for name, value in record["intent"].items() if value is True],
            "pipeline_compliant": record["is_compliant"],
            "guard_violations_now": [f"{v.rule}: {v.message}" for v in guard.check_query(record["sparql_query"])],
        })
    rows.sort(key=lambda row: row["question_id"])
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    flagged = sum(bool(row["guard_violations_now"]) for row in rows)
    print(f"wrote {len(rows)} LLM-written queries to {args.out} ({flagged} with guard violations)")


if __name__ == "__main__":
    main()
