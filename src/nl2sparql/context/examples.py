"""Example bank: question + SPARQL pairs, searched by similarity to the request."""
from __future__ import annotations

import json
from pathlib import Path

from nl2sparql.context.bm25 import BM25, normalize_text, tokenize
from nl2sparql.models import Example


class ExampleBank:
    def __init__(self, examples: list[Example]):
        self.examples = examples
        self.index = BM25([tokenize(example.question) for example in examples])

    @classmethod
    def load(cls, path: str | Path) -> "ExampleBank":
        path = Path(path)
        if not path.exists():
            return cls([])
        examples = []
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
                    examples.append(
                        Example(row["id"], row["question"], row["sparql"], row["compliant"], row.get("source", ""))
                    )
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    raise ValueError(f"Invalid examples file {path} at line {line_number}: {error}") from error
        return cls(examples)

    def search(self, text: str, k: int = 3, exclude_exact: bool = False) -> list[Example]:
        """Top-k examples by BM25. `exclude_exact` drops an example whose question equals the text
        (the eval harness uses it so the official questions do not find their own answer)."""
        if k <= 0 or not self.examples:
            return []
        query = tokenize(text)
        if not query:
            return []
        scores = self.index.scores(query)
        target = normalize_text(text)
        ranked = sorted(range(len(self.examples)), key=lambda i: scores[i], reverse=True)
        picked = []
        for i in ranked:
            if exclude_exact and normalize_text(self.examples[i].question) == target:
                continue
            picked.append(self.examples[i])
            if len(picked) == k:
                break
        return picked
