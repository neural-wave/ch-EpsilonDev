"""Example bank: question + SPARQL pairs, searched by similarity to the request."""
from __future__ import annotations

import json
from pathlib import Path

from nl2sparql.context.bm25 import BM25, fold, tokenize
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
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                examples.append(
                    Example(row["id"], row["question"], row["sparql"], bool(row["compliant"]), row.get("source", ""))
                )
        return cls(examples)

    def search(self, text: str, k: int = 3, exclude_exact: bool = False) -> list[Example]:
        """Top-k examples by BM25. `exclude_exact` drops an example whose question equals the text
        (the eval harness uses it so the official questions do not find their own answer)."""
        if not self.examples:
            return []
        scores = self.index.scores(tokenize(text))
        target = fold(text).strip()
        ranked = sorted(range(len(self.examples)), key=lambda i: scores[i], reverse=True)
        picked = []
        for i in ranked:
            if exclude_exact and fold(self.examples[i].question).strip() == target:
                continue
            picked.append(self.examples[i])
            if len(picked) == k:
                break
        return picked
