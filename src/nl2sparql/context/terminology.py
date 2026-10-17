"""Terminology index: medical terms in the request -> candidate code IRIs.

The index file is built by scripts/build_terminology_index.py from the ontologies loaded in
Virtuoso. It derives from restricted terminologies, so it lives in assets/ and is never committed.
Without it, candidates() returns nothing and the query agent relies on its own knowledge.

Matching: words of the request against words of each label, weighted by how rare a word is among all
labels (every word weighs at least 1, so in a big index "procedure" or "status" count little), with
SNOMED CT semantic tags such as "(finding)" removed and simple plurals folded ("smokers" finds
"Smoker"). A label matched by a single word must be that word or imply the rest of its words
("diabetes" finds "Diabetes mellitus" because the index shows the two always go together; "hospital"
does not find "Death in hospital"). The subcodes of a matched classification code (A00 -> A00.0,
A00.1, A00.9) follow the direct matches. Ties keep the index order, so results are deterministic.

Given the SPHN class names, a code whose label only spells out a class name ("Imaging (procedure)"
for sphn:ImagingProcedure) is dropped: no reference uses such a code, and the model typed the intent
of an imaging procedure with it twice.

The semantic tag also says where a code is used: SchemaCards.code_home() reads it ("(geographic
location)" -> sphn:Country hasCode) for the context line of a candidate and for the judge, which finds
the entry of a typed code by its IRI (by_iri).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from nl2sparql.context.bm25 import tokenize
from nl2sparql.context.schema import words
from nl2sparql.models import CodeEntry

ICD_CODE_RE = re.compile(r"\b([A-Z]\d{2}(?:\.\d{1,2})?)\b")
SEMANTIC_TAG_RE = re.compile(r"\s*\([^()]*\)\s*$")
MAX_LABEL_WORDS = 8
# A word implies another word of a label when they share at least this fraction of the entries the
# first word occurs in. Measured on the dev questions (17 October): "diabetes" implies "mellitus"
# (51 of 54 entries, 0.94); the noise tops out at 0.68 ("dialysis" with "peritoneal" 13 of 19,
# "group" with "chromosome" 8 of 12), and "death" is in 1 of the 3 entries of "hospital" (0.33).
IMPLIED_SHARE = 0.75
# Only independent entries are evidence: a subcode repeats its parent's label, so one family is one
# entry ("kinds" found the German "Kind" of the M41.0 scoliosis family), and a word seen in a single
# entry implies nothing ("least" found "Least restrictive environment").
MIN_IMPLYING_ENTRIES = 2
_CACHE: dict[tuple[str, float], "TerminologyIndex"] = {}


def _word(token: str) -> str:
    return token[:-1] if len(token) > 4 and token.endswith("s") and not token.endswith("ss") else token


def label_words(text: str) -> set[str]:
    return {_word(token) for token in tokenize(SEMANTIC_TAG_RE.sub("", text))}


def class_name_words(class_names: Iterable[str]) -> set[frozenset[str]]:
    """The word sets of the class names made of two or more words: what a code label may not restate
    ("Imaging (procedure)" is sphn:ImagingProcedure; a one-word name such as Death stays a concept)."""
    found = set()
    for name in class_names:
        parts = words(name)
        if len(parts) >= 2:
            found.add(frozenset(_word(token) for token in tokenize(" ".join(parts))))
    return found


def parent_code(code: str) -> str | None:
    """The code one level up in a dotted classification: A00.0 -> A00, E11.01 -> E11.0, 00.0 -> 00."""
    if "." not in code:
        return None
    head, _, tail = code.rpartition(".")
    return head if len(tail) == 1 else code[:-1]


class TerminologyIndex:
    def __init__(self, entries: list[CodeEntry]):
        self.entries = entries
        self.by_code = {entry.code: entry for entry in entries}
        self.by_iri: dict[str, CodeEntry] = {}  # the judge looks up the code a query types (its semantic tag)
        self.position = {}  # first position of each code: the tie-break
        for position, entry in enumerate(entries):
            self.position.setdefault(entry.code, position)
            self.by_iri.setdefault(entry.iri, entry)
        self.labels: list[tuple[int, set[str]]] = []  # (entry position, words of one label)
        self.postings: dict[str, list[int]] = defaultdict(list)
        self.entry_words: list[set[str]] = []  # all label words of each entry
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
            self.entry_words.append(seen)
            frequency.update(seen)
        self.frequency = frequency
        total = len(entries) + 1
        self.weight = {word: max(1.0, math.log(total / (count + 0.5))) for word, count in frequency.items()}
        self.children: dict[str, list[str]] = defaultdict(list)
        self.parent_position: dict[int, int] = {}  # entry position -> position of its parent code
        for position, entry in enumerate(entries):
            parent = parent_code(entry.code)
            if parent in self.by_code:
                self.parent_position[position] = self.position[parent]
                if entry.code not in self.children[parent]:
                    self.children[parent].append(entry.code)
        self._implied: dict[str, set[str]] = {}

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

    def implied(self, word: str) -> set[str]:
        """Words that occur in at least IMPLIED_SHARE of the independent entries `word` occurs in (at least
        MIN_IMPLYING_ENTRIES of them, else only the word itself). Computed on demand because few queries
        need it and the word may occur in thousands of entries."""
        if word not in self._implied:
            positions = {self.labels[label][0] for label in self.postings.get(word, ())}
            independent = [position for position in positions
                           if (parent := self.parent_position.get(position)) is None or word not in self.entry_words[parent]]
            together: Counter[str] = Counter()
            for position in independent:
                together.update(self.entry_words[position])
            self._implied[word] = {word} if len(independent) < MIN_IMPLYING_ENTRIES else {
                other for other, count in together.items() if count >= IMPLIED_SHARE * len(independent)
            }
        return self._implied[word]

    def candidates(self, text: str, k: int = 5, class_names: Iterable[str] = ()) -> list[CodeEntry]:
        """The best k codes for the text; with class_names, none whose label restates a class name."""
        if k <= 0:
            return []
        score_by_code = {code: 10.0 for code in ICD_CODE_RE.findall(text) if code in self.by_code}
        query = label_words(text)
        for label in {label for word in query for label in self.postings.get(word, ())}:
            position, words = self.labels[label]
            shared = words & query
            if not any(len(word) >= 4 for word in shared):
                continue
            if len(shared) == 1:
                if not words <= self.implied(next(iter(shared))):
                    continue
                shared = words  # the one word stands for the whole label
            score = sum(self.weight[w] for w in shared) / sum(self.weight[w] for w in words)
            code = self.entries[position].code
            if score >= 0.5 and score > score_by_code.get(code, 0.0):
                score_by_code[code] = score
        restated = class_name_words(class_names)
        ranked = [code for code in sorted(score_by_code, key=lambda code: (-score_by_code[code], self.position[code]))
                  if not self._restates(code, restated)]
        return [self.by_code[code] for code in self._with_children(ranked[:k], k)]

    def _restates(self, code: str, class_words: set[frozenset[str]]) -> bool:
        """True when one of the code's labels, semantic tag included, is exactly the words of a class name."""
        return bool(class_words) and any(
            frozenset(_word(token) for token in tokenize(label)) in class_words for label in self.by_code[code].labels
        )

    def _with_children(self, codes: list[str], k: int) -> list[str]:
        """The direct matches, then the subcodes the index knows for each of them, as far as k allows.
        A subcode never displaces a direct match."""
        picked = list(codes)
        for code in codes:
            for child in self.children.get(code, ()):
                if len(picked) >= k:
                    return picked
                if child not in picked:
                    picked.append(child)
        return picked
