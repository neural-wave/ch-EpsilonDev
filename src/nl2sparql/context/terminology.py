"""Terminology index: medical terms in the request -> candidate code IRIs.

The index file is built by scripts/build_terminology_index.py from the ontologies loaded in
Virtuoso. It derives from restricted terminologies, so it lives in assets/ and is never committed.
Without it, candidates() returns nothing and the query agent relies on its own knowledge.

Matching: words of the request against words of each label, weighted by how rare a word is among all
labels (so "procedure" or "status" count little), with SNOMED CT semantic tags such as "(finding)"
removed and simple plurals folded ("smokers" finds "Smoker").
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
        self.weight = {word: math.log(total / (count + 0.5)) for word, count in frequency.items()}

    @classmethod
    def load(cls, path: str | Path) -> "TerminologyIndex":
        """Reads the index once per file version: it has tens of thousands of entries."""
        path = Path(path)
        if not path.exists():
            return cls([])
        key = (str(path.resolve()), path.stat().st_mtime)
        if key not in _CACHE:
            entries = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    entries.append(CodeEntry(row["iri"], row["code"], row["system"], tuple(row["labels"])))
            _CACHE[key] = cls(entries)
        return _CACHE[key]

    def candidates(self, text: str, k: int = 5) -> list[CodeEntry]:
        found: dict[str, tuple[float, CodeEntry]] = {}
        for code in ICD_CODE_RE.findall(text):
            if code in self.by_code:
                found[code] = (10.0, self.by_code[code])
        query = label_words(text)
        touched = {label for word in query for label in self.postings.get(word, ())}
        for label in touched:
            position, words = self.labels[label]
            shared = words & query
            if not any(len(word) >= 4 for word in shared):
                continue
            score = sum(self.weight[w] for w in shared) / sum(self.weight[w] for w in words)
            entry = self.entries[position]
            if score >= 0.5 and score > found.get(entry.code, (0.0, None))[0]:
                found[entry.code] = (score, entry)
        ranked = sorted(found.values(), key=lambda pair: (-pair[0], len(pair[1].code)))
        return [entry for _, entry in ranked[:k]]
