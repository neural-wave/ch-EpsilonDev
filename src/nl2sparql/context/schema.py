"""Schema cards: the SPHN classes that really exist in the data, with their properties."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from nl2sparql.context.bm25 import fold, tokenize

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
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid schema cards file {path}: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid schema cards file {path}: root must be a JSON object")
        for field in ("classes", "schema_terms"):
            if field not in payload:
                raise ValueError(f"Invalid schema cards file {path}: missing required field {field!r}")
        classes, schema_terms = payload.get("classes", {}), payload.get("schema_terms", [])
        if not isinstance(classes, dict):
            raise ValueError(f"Invalid schema cards file {path}: 'classes' must be an object")
        if not isinstance(schema_terms, list) or any(not isinstance(term, str) for term in schema_terms):
            raise ValueError(f"Invalid schema cards file {path}: 'schema_terms' must be a list of strings")
        for name, card in classes.items():
            if not isinstance(name, str) or not isinstance(card, dict):
                raise ValueError(f"Invalid schema cards file {path}: each class must map a string name to an object")
            if not isinstance(card.get("instances"), int) or isinstance(card.get("instances"), bool):
                raise ValueError(f"Invalid schema cards file {path}: class {name!r} needs integer 'instances'")
            if not isinstance(card.get("patients"), int) or isinstance(card.get("patients"), bool):
                raise ValueError(f"Invalid schema cards file {path}: class {name!r} needs integer 'patients'")
            properties = card.get("properties")
            if not isinstance(properties, dict):
                raise ValueError(f"Invalid schema cards file {path}: class {name!r} needs object 'properties'")
            for prop, info in properties.items():
                if not isinstance(prop, str) or not isinstance(info, dict):
                    raise ValueError(f"Invalid schema cards file {path}: properties must map string names to objects")
                if "kind" in info and not isinstance(info["kind"], str):
                    raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid 'kind'")
                if "count" in info and (not isinstance(info["count"], int) or isinstance(info["count"], bool)):
                    raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid 'count'")
                for field in ("target", "datatype"):
                    if field in info and not isinstance(info[field], str):
                        raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid {field!r}")
        return cls(classes, schema_terms)

    @property
    def empty(self) -> bool:
        return not self.classes

    def in_data(self, term: str) -> bool:
        """True when the class or property occurs in the patient data (always True without cards)."""
        return self.empty or term in self.data_terms

    def closest(self, term: str) -> str | None:
        scored = [
            (difflib.SequenceMatcher(None, term, candidate).ratio(), candidate)
            for candidate in self.data_terms
        ]
        matches = [(score, candidate) for score, candidate in scored if score > 0.6]
        # Match get_close_matches' existing tie choice for the sorted candidate set.
        return max(matches, key=lambda item: (item[0], item[1]))[1] if matches else None

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
        if k <= 0 or self.empty or not tokenize(text):
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
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [self.render(name) for _, _, name in scored[:k]]
