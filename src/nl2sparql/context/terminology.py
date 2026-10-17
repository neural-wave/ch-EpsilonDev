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
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError(f"Could not read terminology index file {path}: {error}") from error
        for line_number, line in enumerate(lines, start=1):
            if line.strip():
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("row must be a JSON object")
                    for field in ("iri", "code", "system", "labels"):
                        if field not in row:
                            raise ValueError(f"missing required field {field!r}")
                    for field in ("iri", "code", "system"):
                        if not isinstance(row[field], str):
                            raise ValueError(f"field {field!r} must be a string")
                    if not isinstance(row["labels"], list) or any(not isinstance(label, str) for label in row["labels"]):
                        raise ValueError("field 'labels' must be a list of strings")
                    entries.append(CodeEntry(row["iri"], row["code"], row["system"], tuple(row["labels"])))
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    raise ValueError(f"Invalid terminology index file {path} at line {line_number}: {error}") from error
        return cls(entries)

    def candidates(self, text: str, k: int = 5) -> list[CodeEntry]:
        if k <= 0:
            return []
        query = set(tokenize(text))
        mentioned_codes = set(ICD_CODE_RE.findall(text))
        if not query and not (mentioned_codes & self.by_code.keys()):
            return []

        score_by_code = {code: 10.0 for code in mentioned_codes if code in self.by_code}
        for entry, label_sets in zip(self.entries, self.label_tokens):
            best = 0.0
            for tokens in label_sets:
                if tokens and len(tokens) <= 8:
                    overlap = len(tokens & query)
                    if overlap and any(len(token) >= 4 for token in tokens & query):
                        best = max(best, overlap / len(tokens))
            if best >= 0.5 and best > score_by_code.get(entry.code, 0.0):
                score_by_code[entry.code] = best

        # Keep the first occurrence of each code as its stable source-order position. Use the
        # canonical entry from by_code, while scoring any duplicate rows' labels above.
        source_order = dict.fromkeys(entry.code for entry in self.entries)
        ranked = sorted(
            ((score_by_code[code], self.by_code[code]) for code in source_order if code in score_by_code),
            key=lambda pair: -pair[0],
        )
        return [entry for _, entry in ranked[:k]]
