"""Collect every IRI a parsed query mentions, including inside property paths."""
from __future__ import annotations

from rdflib import URIRef
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath


def collect_iris(node: object) -> set[URIRef]:
    found: set[URIRef] = set()
    _walk(node, found)
    return found


def _walk(node: object, found: set[URIRef]) -> None:
    if isinstance(node, URIRef):
        found.add(node)
    elif isinstance(node, (SequencePath, AlternativePath, NegatedPath)):
        _walk(node.args, found)
    elif isinstance(node, MulPath):
        _walk(node.path, found)
    elif isinstance(node, InvPath):
        _walk(node.arg, found)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            _walk(item, found)
    elif isinstance(node, dict):  # CompValue nodes and VALUES rows
        for key, value in node.items():
            if isinstance(key, str) and key.startswith("_"):
                continue
            _walk(value, found)
