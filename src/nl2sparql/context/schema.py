"""Schema cards: the SPHN classes that really exist in the data, with their properties.

Optional value sets (assets/value_sets.json, built by scripts/build_value_sets.py) add the few coded
values a property takes in the data, e.g. the six SNOMED CT concepts of CivilStatus.hasCode. They
derive from restricted terminologies, so they live in assets/ and are never committed."""
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
    def __init__(self, classes: dict[str, dict], schema_terms: list[str], value_sets: dict | None = None):
        self.classes = classes
        self.schema_terms = set(schema_terms)
        self.data_terms = set(classes) | {prop for card in classes.values() for prop in card["properties"]}
        self.value_sets = value_sets or {}  # class -> property -> [{"value", "label", "count"}]

    @classmethod
    def load(cls, path: str | Path, value_sets_path: str | Path | None = None) -> "SchemaCards":
        path = Path(path)
        if not path.exists():
            return cls({}, [])
        payload = json.loads(path.read_text(encoding="utf-8"))
        value_sets = {}
        if value_sets_path is not None and Path(value_sets_path).exists():
            value_sets = json.loads(Path(value_sets_path).read_text(encoding="utf-8")).get("classes", {})
        return cls(payload.get("classes", {}), payload.get("schema_terms", []), value_sets)

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
            values = self.value_sets.get(name, {}).get(prop)
            if values and all(v["value"].startswith("sphn-individual:") for v in values):
                parts.append(f"{prop} = one of " + " | ".join(v["value"] for v in values))
            elif values:  # code nodes: ?x sphn:<prop> ?c . ?c a <value>
                parts.append(f"{prop} -> code typed as one of " + " | ".join(f"{v['label']} {v['value']}" for v in values))
            elif "target" in info:
                parts.append(f"{prop} -> sphn:{info['target']}")
            elif info.get("datatype"):
                parts.append(f"{prop} ({info['datatype']})")
            else:
                parts.append(f"{prop} ({KIND_LABELS.get(info.get('kind'), '?')})")
        return f"sphn:{name} [{card['instances']} records, {card['patients']} patients]: " + "; ".join(parts)

    def cards_for(self, text: str, k: int = 6, examples_sparql: str = "") -> list[str]:
        """Cards for the classes the question names (as words or as sphn:Name) first, then the classes
        the retrieved examples use."""
        if self.empty:
            return []
        folded = fold(text)
        named = {term for term in SPHN_TERM_RE.findall(text) if term in self.classes}
        used_by_examples = {term for term in SPHN_TERM_RE.findall(examples_sparql) if term in self.classes}
        scored = []
        for name in self.classes:
            parts = words(name)
            hits = sum(1 for part in parts if len(part) > 2 and part in folded)
            # Half credit for a shared stem: "resuscitate" points at ResuscitationDirective.
            hits += 0.5 * sum(1 for part in parts if len(part) >= 7 and part not in folded and part[:6] in folded)
            in_question = name in named or hits / len(parts) >= 0.5
            score = (1 + hits / len(parts) if in_question else 0) + (2 if name in named else 0) \
                + (0.5 if name in used_by_examples else 0)
            if score > 0:
                scored.append((score, hits, name))  # ties: the class matching more words wins
        scored.sort(reverse=True)
        return [self.render(name) for _, _, name in scored[:k]]
