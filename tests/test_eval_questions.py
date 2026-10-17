import json
from pathlib import Path

import pytest

from nl2sparql.guard.decision import FLAG_RULES

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"


def load(name):
    return [json.loads(line) for line in (EVAL_DIR / name).read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("name", ["questions.jsonl", "intent_questions.jsonl"])
def test_eval_questions_are_well_formed(name):
    for row in load(name):
        assert row["question"].strip() and isinstance(row["expected_compliant"], bool), row["id"]
        first_word = row["category"].split(" ")[0]
        # The intent eval reads the rule of a forbidden question from the first word of its category.
        if row["expected_compliant"]:
            assert first_word == "allowed", row["id"]
        else:
            assert first_word in FLAG_RULES.values(), row["id"]


def test_eval_question_ids_are_unique_across_files():
    ids = [row["id"] for name in ("questions.jsonl", "intent_questions.jsonl") for row in load(name)]
    assert len(ids) == len(set(ids))
