"""Terminology index: medical terms in the request -> candidate code IRIs.

The index file is built by scripts/build_terminology_index.py from the ontologies loaded in
Virtuoso. It derives from restricted terminologies, so it lives in assets/ and is never committed.
Without it, candidates() returns nothing and the query agent relies on its own knowledge.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from nl2sparql.context.bm25 import tokenize
from nl2sparql.models import CodeEntry

ICD_CODE_RE = re.compile(r"\b([A-Z]\d{2}(?:\.\d{1,2})?)\b")


class TerminologyIndex:
    def __init__(self, entries: list[CodeEntry]):
        self.entries = entries
        self.by_code = {entry.code: entry for entry in entries}
        self.label_tokens = [[set(tokenize(label)) for label in entry.labels] for entry in entries]

    @classmethod
    def load(cls, path: str | Path) -> "TerminologyIndex":
        path = Path(path)
        if not path.exists():
            return cls([])
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                entries.append(CodeEntry(row["iri"], row["code"], row["system"], tuple(row["labels"])))
        return cls(entries)

    def candidates(self, text: str, k: int = 5) -> list[CodeEntry]:
        found: dict[str, tuple[float, CodeEntry]] = {}
        for code in ICD_CODE_RE.findall(text):
            if code in self.by_code:
                found[code] = (10.0, self.by_code[code])
        query = set(tokenize(text))
        for entry, label_sets in zip(self.entries, self.label_tokens):
            best = 0.0
            for tokens in label_sets:
                if tokens and len(tokens) <= 8:
                    overlap = len(tokens & query)
                    if overlap and any(len(token) >= 4 for token in tokens & query):
                        best = max(best, overlap / len(tokens))
            if best >= 0.5 and best > found.get(entry.code, (0.0, None))[0]:
                found[entry.code] = (best, entry)
        ranked = sorted(found.values(), key=lambda pair: (-pair[0], len(pair[1].code)))
        return [entry for _, entry in ranked[:k]]
