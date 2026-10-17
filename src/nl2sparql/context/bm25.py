"""Tiny BM25 ranking, enough to pick similar examples without extra dependencies."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by did do does for from has have how in is it its many much of on or "
    "our the their them they this to was we what when which who with".split()
)


def fold(text: str) -> str:
    """Lowercase and strip accents, so 'Sjögren' matches 'sjogren'."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    """Fold case and accents and collapse whitespace for exact-text comparisons."""
    return " ".join(fold(text).split())


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(fold(text)) if token not in STOPWORDS]


class BM25:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.documents = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.average_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.k1, self.b = k1, b
        document_frequency = Counter(token for doc in self.documents for token in doc)
        total = len(documents)
        self.idf = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5)) for token, freq in document_frequency.items()
        }

    def scores(self, query: list[str]) -> list[float]:
        results = []
        for counts, length in zip(self.documents, self.lengths):
            score = 0.0
            for token in query:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                norm = self.k1 * (1 - self.b + self.b * length / (self.average_length or 1))
                score += self.idf[token] * frequency * (self.k1 + 1) / (frequency + norm)
            results.append(score)
        return results
