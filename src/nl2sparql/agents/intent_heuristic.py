"""Baseline intent agent: keyword rules, no model, 0 tokens. The LLM version replaces it."""
from __future__ import annotations

import re

from nl2sparql.models import IntentFlags

PATTERNS = {
    "asks_for_identifiers": re.compile(
        r"\b(which (ones|patients|cases)|who (are|is|were) (they|the)|identifiers?|\bids?\b|pseudo.?ids?|"
        r"names? of (the )?patients|list (the |all )?patients)\b", re.IGNORECASE),
    "single_patient": re.compile(
        r"\b(patient\s+(number\s+|no\.?\s*|#\s*)?[A-Za-z_-]*\d+|this patient|that patient|a specific patient|"
        r"one patient)\b", re.IGNORECASE),
    "individual_rows": re.compile(
        r"\b(show me (each|every|all)|list (each|every|all)|each (case|record|row)|one row per|"
        r"row by row|for each patient)\b", re.IGNORECASE),
    "whole_graph": re.compile(
        r"\b(how much data|all (the )?data|everything|entire (database|graph)|whole (database|graph)|"
        r"all triples|dump)\b", re.IGNORECASE),
    "not_a_select": re.compile(
        r"\b(delete|remove|insert|update|drop|modify|as a graph|construct|take away|export)\b", re.IGNORECASE),
}
REASONS = {
    "asks_for_identifiers": "asks which patients or records are involved",
    "single_patient": "is about one specific patient",
    "individual_rows": "asks for individual records instead of a summary",
    "whole_graph": "asks for the whole database without restriction",
    "not_a_select": "asks to change or export data",
}
IDENTIFIER_TAIL = re.compile(r",?\s*(and\s+)?(which ones|who) (are|were) (they|them)\??\s*$", re.IGNORECASE)


class HeuristicIntentAgent:
    async def classify(self, text: str) -> tuple[IntentFlags, int]:
        raised = {name: bool(pattern.search(text)) for name, pattern in PATTERNS.items()}
        if not any(raised.values()):
            return IntentFlags(), 0
        first = next(name for name, value in raised.items() if value)
        rephrase = None
        if raised["asks_for_identifiers"] and IDENTIFIER_TAIL.search(text):
            rephrase = IDENTIFIER_TAIL.sub("", text).rstrip(" ,?") + "?"
        return IntentFlags(**raised, reason=REASONS[first], suggested_rephrase=rephrase), 0
