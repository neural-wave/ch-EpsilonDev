"""Compare two RDF-derived profile directories and print sanitized aggregate counts.

This script reports counts only. It does not print class/property names or profile entries.
Use it after scripts/profile_data.py has written a candidate profile to a temporary directory:

    python scripts/compare_data_profile.py --profile-dir /tmp/profile-output
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nl2sparql.config import REPO_ROOT


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read valid JSON from {path}: {error}") from error


def _load_profile(directory: Path) -> tuple[dict, dict, set[str]]:
    kinds = _load_json(directory / "property_kinds.json")
    cards = _load_json(directory / "schema_cards.json")
    if not isinstance(kinds, dict) or not isinstance(cards, dict):
        raise ValueError(f"Profile in {directory} must contain JSON objects")
    classes = cards.get("classes")
    terms = cards.get("schema_terms")
    if not isinstance(classes, dict) or not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
        raise ValueError(f"Schema profile in {directory} has an invalid classes/schema_terms structure")
    if not kinds or not classes or not terms:
        raise ValueError(f"Profile in {directory} is empty or incomplete")
    for class_name, card in classes.items():
        if not isinstance(class_name, str) or not isinstance(card, dict) or not isinstance(card.get("properties"), dict):
            raise ValueError(f"Schema profile in {directory} has an invalid class-card structure")
        if any(not isinstance(prop, str) or not isinstance(info, dict) for prop, info in card["properties"].items()):
            raise ValueError(f"Schema profile in {directory} has an invalid property-card structure")
    if any(not isinstance(iri, str) or not isinstance(info, dict) for iri, info in kinds.items()):
        raise ValueError(f"Property-kind profile in {directory} has an invalid entry structure")
    return kinds, classes, set(terms)


def _map_delta(before: dict, after: dict) -> tuple[int, int, int]:
    before_keys, after_keys = set(before), set(after)
    added = len(after_keys - before_keys)
    removed = len(before_keys - after_keys)
    changed = sum(before[key] != after[key] for key in before_keys & after_keys)
    return added, removed, changed


def _property_delta(before: dict, after: dict) -> tuple[int, int, int]:
    before_properties = {
        (class_name, prop): info
        for class_name, card in before.items()
        for prop, info in card["properties"].items()
    }
    after_properties = {
        (class_name, prop): info
        for class_name, card in after.items()
        for prop, info in card["properties"].items()
    }
    return _map_delta(before_properties, after_properties)


def _print_delta(label: str, before_count: int, after_count: int, delta: tuple[int, int, int]) -> None:
    added, removed, changed = delta
    print(
        f"{label}: baseline={before_count} profile={after_count} "
        f"added={added} removed={removed} changed={changed}"
    )


def compare(baseline_dir: Path, profile_dir: Path) -> bool:
    baseline_kinds, baseline_classes, baseline_terms = _load_profile(baseline_dir)
    profile_kinds, profile_classes, profile_terms = _load_profile(profile_dir)

    kind_delta = _map_delta(baseline_kinds, profile_kinds)
    class_delta = _map_delta(baseline_classes, profile_classes)
    property_delta = _property_delta(baseline_classes, profile_classes)
    term_delta = (
        len(profile_terms - baseline_terms),
        len(baseline_terms - profile_terms),
        0,
    )

    _print_delta("properties", len(baseline_kinds), len(profile_kinds), kind_delta)
    _print_delta("classes", len(baseline_classes), len(profile_classes), class_delta)
    _print_delta(
        "class_property_pairs",
        sum(len(card["properties"]) for card in baseline_classes.values()),
        sum(len(card["properties"]) for card in profile_classes.values()),
        property_delta,
    )
    _print_delta("schema_terms", len(baseline_terms), len(profile_terms), term_delta)

    matches = not any((*kind_delta, *class_delta, *property_delta, *term_delta))
    print(f"profiles_match: {str(matches).lower()}")
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--profile-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        matches = compare(args.baseline_dir, args.profile_dir)
    except ValueError as error:
        parser.error(str(error))
    raise SystemExit(0 if matches else 1)


if __name__ == "__main__":
    main()
