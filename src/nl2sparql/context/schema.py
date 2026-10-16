"""Schema cards: the SPHN classes that really exist in the data, with their properties."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from nl2sparql.context.bm25 import fold

CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
SPHN_TERM_RE = re.compile(r"sphn:([A-Za-z]+)")
KIND_LABELS = {"literal": "value", "individual": "value set", "resource": "link", "mixed": "mixed", "unknown": "?"}


def words(name: str) -> list[str]:
    return [part.lower() for part in CAMEL_RE.findall(name)]


class SchemaCards:
    def __init__(self, classes: dict[str, dict], schema_terms: list[str]):
        self.classes = classes
        self.schema_terms = set(schema_terms)
        self.data_terms = set(classes) | {prop for card in classes.values() for prop in card["properties"]}

    @classmethod
    def load(cls, path: str | Path) -> "SchemaCards":
        path = Path(path)
        if not path.exists():
            return cls({}, [])
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(payload.get("classes", {}), payload.get("schema_terms", []))

    @property
    def empty(self) -> bool:
        return not self.classes

    def in_data(self, term: str) -> bool:
        """True when the class or property occurs in the patient data (always True without cards)."""
        return self.empty or term in self.data_terms

    def closest(self, term: str) -> str | None:
        matches = difflib.get_close_matches(term, sorted(self.data_terms), n=1, cutoff=0.6)
        return matches[0] if matches else None

    def render(self, name: str) -> str:
        card = self.classes[name]
        parts = []
        for prop, info in card["properties"].items():
            if "target" in info:
                parts.append(f"{prop} -> sphn:{info['target']}")
            elif info.get("datatype"):
                parts.append(f"{prop} ({info['datatype']})")
            else:
                parts.append(f"{prop} ({KIND_LABELS.get(info.get('kind'), '?')})")
        return f"sphn:{name} [{card['instances']} records, {card['patients']} patients]: " + "; ".join(parts)

    def cards_for(self, text: str, k: int = 6) -> list[str]:
        """Cards for classes named in the text (as words or as sphn:Name), best matches first."""
        if self.empty:
            return []
        folded = fold(text)
        mentioned = {term for term in SPHN_TERM_RE.findall(text) if term in self.classes}
        scored = []
        for name in self.classes:
            parts = words(name)
            hits = sum(1 for part in parts if len(part) > 2 and part in folded)
            score = hits / len(parts) + (2 if name in mentioned else 0)
            if score >= 0.5:
                scored.append((score, hits, name))  # ties: the class matching more words wins
        scored.sort(reverse=True)
        return [self.render(name) for _, _, name in scored[:k]]
