"""Terminology index: medical terms in the request -> candidate code IRIs.

The index file is built by scripts/build_terminology_index.py from the ontologies loaded in
Virtuoso. It derives from restricted terminologies, so it lives in assets/ and is never committed.
Without it, candidates() returns nothing and the query agent relies on its own knowledge.

Matching: words of the request against words of each label, weighted by how rare a word is among all
labels (every word weighs at least 1, so in a big index "procedure" or "status" count little), with
SNOMED CT semantic tags such as "(finding)" removed and simple plurals folded ("smokers" finds
"Smoker"). Ties keep the index order, so results are deterministic.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from nl2sparql.context.bm25 import tokenize
from nl2sparql.models import CodeEntry

ICD_CODE_RE = re.compile(r"\b([A-Z]\d{2}(?:\.\d{1,2})?)\b")
SEMANTIC_TAG_RE = re.compile(r"\s*\([^()]*\)\s*$")
MAX_LABEL_WORDS = 8
_CACHE: dict[tuple[str, float], "TerminologyIndex"] = {}


def _word(token: str) -> str:
    return token[:-1] if len(token) > 4 and token.endswith("s") and not token.endswith("ss") else token


def label_words(text: str) -> set[str]:
    return {_word(token) for token in tokenize(SEMANTIC_TAG_RE.sub("", text))}


class TerminologyIndex:
    def __init__(self, entries: list[CodeEntry]):
        self.entries = entries
        self.by_code = {entry.code: entry for entry in entries}
        self.position = {}  # first position of each code: the tie-break
        for position, entry in enumerate(entries):
            self.position.setdefault(entry.code, position)
        self.labels: list[tuple[int, set[str]]] = []  # (entry position, words of one label)
        self.postings: dict[str, list[int]] = defaultdict(list)
        frequency: Counter[str] = Counter()
        for position, entry in enumerate(entries):
            seen: set[str] = set()
            for label in entry.labels:
                found = label_words(label)
                if not found or len(found) > MAX_LABEL_WORDS:
                    continue
                for word in found:
                    self.postings[word].append(len(self.labels))
                self.labels.append((position, found))
                seen |= found
            frequency.update(seen)
        total = len(entries) + 1
        self.weight = {word: max(1.0, math.log(total / (count + 0.5))) for word, count in frequency.items()}

    @classmethod
    def load(cls, path: str | Path) -> "TerminologyIndex":
        """Reads and validates the index once per file version: it has tens of thousands of entries."""
        path = Path(path)
        if not path.exists():
            return cls([])
        key = (str(path.resolve()), path.stat().st_mtime)
        if key in _CACHE:
            return _CACHE[key]
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
        _CACHE[key] = cls(entries)
        return _CACHE[key]

    def candidates(self, text: str, k: int = 5) -> list[CodeEntry]:
        if k <= 0:
            return []
        score_by_code = {code: 10.0 for code in ICD_CODE_RE.findall(text) if code in self.by_code}
        query = label_words(text)
        for label in {label for word in query for label in self.postings.get(word, ())}:
            position, words = self.labels[label]
            shared = words & query
            if not any(len(word) >= 4 for word in shared):
                continue
            score = sum(self.weight[w] for w in shared) / sum(self.weight[w] for w in words)
            code = self.entries[position].code
            if score >= 0.5 and score > score_by_code.get(code, 0.0):
                score_by_code[code] = score
        ranked = sorted(score_by_code, key=lambda code: (-score_by_code[code], self.position[code]))
        return [self.by_code[code] for code in ranked[:k]]
