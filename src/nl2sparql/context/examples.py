"""Example bank: question + SPARQL pairs, searched by similarity to the request.

Two files feed the bank: data/examples.jsonl (the official examples, which the eval scripts also read
as their questions) and, when it exists next to it, data/team_examples.jsonl (retrieval-only examples
the team wrote). The eval scripts read examples.jsonl only, so a team example never becomes an eval
question.

With CONTEXT_DEV_REFERENCES=true a third source follows: the dev rows of eval/query_questions.jsonl
(question + reference SPARQL), loaded last so an official example wins a tie. Only rows whose split is
"dev" are read; the holdout rows are a blind exam and never enter the bank. The eval excludes a
question's own example by its text, so a dev question then retrieves its neighbours' references: with the
flag on, only the holdout numbers of the eval are meaningful.

A record may carry a "shape" tag (plain, conditional_count, two_cohort, breakdown). BM25 on words cannot
see shapes, so search() makes sure a question that asks for a share or compares two cohorts sees one
example of that shape: it swaps the last retrieved example for the best-scoring one of the needed shape.
The dev references carry no tag: reference_shape() reads it from their SPARQL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from nl2sparql.context.bm25 import BM25, normalize_text, tokenize
from nl2sparql.models import Example

TEAM_EXAMPLES_FILE = "team_examples.jsonl"
DEV_REFERENCE_SOURCE = "dev reference"
SHAPES = ("plain", "conditional_count", "two_cohort", "breakdown")
# Cue phrases in the question -> the shape the question needs. A cohort comparison wins over a share:
# the two-cohort example already shows the conditional count inside each cohort.
SHAPE_CUES = (
    ("two_cohort", re.compile(r"\bthan other patients\b|\bcompared (?:with|to)\b|\bversus\b|\bvs\b")),
    ("conditional_count", re.compile(
        r"\bhow often\b|\bhow many of\b|\bshare\b|\bwhat usually happens\b|\bpercentage\b|\bproportion\b|\bwhat fraction\b"
    )),
)


# What a reference query looks like, for reference_shape().
COHORT_RE = re.compile(r"\bBIND\s*\(\s*IF\s*\(\s*EXISTS\b", re.IGNORECASE)
OPTIONAL_RE = re.compile(r"\bOPTIONAL\s*\{", re.IGNORECASE)
# BIND(?g AS ?x) counts the patients that also match the OPTIONAL pattern, BIND(?record AS ?x) the records.
SUBSET_BIND_RE = re.compile(r"\bBIND\s*\(\s*\?\w+\s+AS\s+\?\w+\s*\)", re.IGNORECASE)
WHERE_RE = re.compile(r"\bWHERE\s*\{", re.IGNORECASE)
GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def needed_shape(text: str) -> str | None:
    folded = normalize_text(text)
    for shape, cue in SHAPE_CUES:
        if cue.search(folded):
            return shape
    return None


def reference_shape(sparql: str) -> str:
    """The shape tag of a query, by simple rules on its text, for rows written without one.

    BIND(IF(EXISTS ...)) -> two_cohort; an OPTIONAL block with BIND(?g AS ?x), or BIND(?record AS ?x)
    for a share of records -> conditional_count; a GROUP BY of the outer query -> breakdown (GROUP BY ?g
    in a sub-select counts per patient, it is no breakdown); else plain. A query with both of the first
    two is a cohort comparison, as in SHAPE_CUES.
    """
    if COHORT_RE.search(sparql):
        return "two_cohort"
    for optional in OPTIONAL_RE.finditer(sparql):
        if SUBSET_BIND_RE.search(sparql, optional.end() - 1, _block_end(sparql, optional.end() - 1)):
            return "conditional_count"
    where = WHERE_RE.search(sparql)
    start = where.end() - 1 if where else sparql.find("{")
    if start >= 0 and GROUP_BY_RE.search(sparql, _block_end(sparql, start)):
        return "breakdown"
    return "plain"


def _block_end(text: str, start: int) -> int:
    """The index after the brace that closes the one at `start` (the end of the text if none does)."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


class ExampleBank:
    def __init__(self, examples: list[Example], shapes: list[str] | None = None):
        self.examples = examples
        self.shapes = list(shapes) if shapes is not None else ["plain"] * len(examples)
        if len(self.shapes) != len(examples):
            raise ValueError("shapes must have one entry per example")
        self.index = BM25([tokenize(example.question) for example in examples])

    @classmethod
    def load(cls, path: str | Path, dev_references: str | Path | None = None) -> "ExampleBank":
        """Loads the official file and, when it exists next to it, the team's retrieval-only file;
        then the dev rows of `dev_references` (eval/query_questions.jsonl) when given and the file exists."""
        path = Path(path)
        files = [path]
        if path.name != TEAM_EXAMPLES_FILE:
            files.append(path.with_name(TEAM_EXAMPLES_FILE))
        examples, shapes = [], []
        for file in files:
            if file.exists():
                _read(file, examples, shapes)
        if dev_references is not None and Path(dev_references).exists():
            _read_dev_references(Path(dev_references), examples, shapes)
        return cls(examples, shapes)

    def search(self, text: str, k: int = 3, exclude_exact: bool = False) -> list[Example]:
        """Top-k examples by BM25. `exclude_exact` drops an example whose question equals the text
        (the eval harness uses it so an official question or a dev reference does not find its own answer).
        A question that needs a shape (see SHAPE_CUES) gets one example of it in the last slot."""
        if k <= 0 or not self.examples:
            return []
        query = tokenize(text)
        if not query:
            return []
        scores = self.index.scores(query)
        target = normalize_text(text)
        # sorted() is stable: on equal scores the load order wins (official, team, then dev references).
        ranked = [i for i in sorted(range(len(self.examples)), key=lambda i: scores[i], reverse=True)
                  if not (exclude_exact and normalize_text(self.examples[i].question) == target)]
        picked = ranked[:k]
        shape = needed_shape(text)
        # The top example is never evicted: with one slot the best-scoring example wins.
        if shape and len(picked) >= 2 and all(self.shapes[i] != shape for i in picked):
            best = next((i for i in ranked if self.shapes[i] == shape), None)
            if best is not None:
                picked[-1] = best
        return [self.examples[i] for i in picked]


def _read(path: Path, examples: list[Example], shapes: list[str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"Could not read examples file {path}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if line.strip():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row must be a JSON object")
                for field in ("id", "question", "sparql", "compliant"):
                    if field not in row:
                        raise ValueError(f"missing required field {field!r}")
                for field in ("id", "question", "sparql"):
                    if not isinstance(row[field], str):
                        raise ValueError(f"field {field!r} must be a string")
                if not isinstance(row["compliant"], bool):
                    raise ValueError("field 'compliant' must be a boolean")
                if "source" in row and not isinstance(row["source"], str):
                    raise ValueError("field 'source' must be a string")
                if row.get("shape", "plain") not in SHAPES:
                    raise ValueError(f"field 'shape' must be one of {', '.join(SHAPES)}")
                examples.append(
                    Example(row["id"], row["question"], row["sparql"], row["compliant"], row.get("source", ""))
                )
                shapes.append(row.get("shape", "plain"))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid examples file {path} at line {line_number}: {error}") from error


def _read_dev_references(path: Path, examples: list[Example], shapes: list[str]) -> None:
    """The dev rows of a file of eval questions with reference SPARQL (eval/query_questions.jsonl).

    A row is kept only when its split is exactly "dev"; any other row, a holdout row or one without a
    split, is skipped before its other fields are read, and no error message quotes a row.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"Could not read dev references file {path}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if line.strip():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row must be a JSON object")
                if row.get("split") != "dev":
                    continue
                for field in ("id", "question", "reference_sparql"):
                    if not isinstance(row.get(field), str):
                        raise ValueError(f"field {field!r} must be a string")
                examples.append(Example(row["id"], row["question"], row["reference_sparql"], True, DEV_REFERENCE_SOURCE))
                shapes.append(reference_shape(row["reference_sparql"]))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid dev references file {path} at line {line_number}: {error}") from error
