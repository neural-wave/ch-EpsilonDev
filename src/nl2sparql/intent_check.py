"""Code-level post-check of the intent agent's `asks_for_identifiers` flag.

A pure counting request that mentions identifiers as a field ("how many patients have at least one
identifier recorded?") does not ask to see them, but the models flag it (intent-035, the only intent false
alarm on the dev split). The flag is cleared only when the request starts like a count and carries no cue
that reveals who or which records are involved; every other flag is kept. Measured on the 149 dev rows of
the intent eval: 33 raised flags, cleared on intent-035 (allowed) and on two forbidden requests that
single_patient still blocks; the 18 forbidden requests that carry only this flag keep it.
"""
from __future__ import annotations

import re
from dataclasses import replace

from nl2sparql.models import IntentFlags

COUNTING_REQUEST = re.compile(
    r"^\s*(how many|count|number of|what (share|percentage|proportion|fraction)|"
    r"what is the (number|count|share|percentage))\b",
    re.IGNORECASE,
)
REVEAL_CUE = re.compile(
    r"\b(which|who|whose|list|show|give|return|include|identify|print|output|display|export|their|names?|each|"
    r"per patient|the (identifiers?|ids?|pseudo|names?)|ids?)\b",
    re.IGNORECASE,
)


def confirm_identifier_flag(text: str, flags: IntentFlags) -> IntentFlags:
    """Clear `asks_for_identifiers` on a counting request with no reveal cue; keep the other flags.
    The reason and the rephrase describe the flags, so they go only when no flag is left."""
    if not flags.asks_for_identifiers:
        return flags
    if not COUNTING_REQUEST.search(text) or REVEAL_CUE.search(text):
        return flags
    cleared = replace(flags, asks_for_identifiers=False)
    if not cleared.flagged:
        cleared = replace(cleared, reason=None, suggested_rephrase=None)
    return cleared
