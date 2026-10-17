"""Proposed code-level post-check for the intent agent's `asks_for_identifiers` flag (intent-035).

Drops the flag when the request is a pure count that mentions identifiers as a field, not as something to
see. Keeps every other flag. Evaluated on the dev rows of eval/reports/intent-20261017-044230.json:
33 raised -> dropped on intent-035 (allowed) and on official-11 / intent-001 (forbidden, still blocked by
single_patient); all 18 true positives that carry ONLY asks_for_identifiers keep it.

Where it goes: nl2sparql/orchestrator.py `_classify`, right after the flags come back (or in
LLMIntentAgent.classify_detailed before returning). Not a prompt change.
"""
from __future__ import annotations

import re
from dataclasses import replace

from nl2sparql.models import IntentFlags

COUNTING_REQUEST = re.compile(
    r"^\s*(how many|count|number of|what (share|percentage|proportion|fraction)|what is the (number|count|share|percentage))\b",
    re.IGNORECASE,
)
REVEAL_CUE = re.compile(
    r"\b(which|who|whose|list|show|give|return|include|identify|print|output|display|export|their|names?|each|"
    r"per patient|the (identifiers?|ids?|pseudo|names?)|ids?)\b",
    re.IGNORECASE,
)


def confirm_identifier_flag(text: str, flags: IntentFlags) -> IntentFlags:
    """A count that only mentions identifiers ("have at least one identifier recorded") does not ask to see them."""
    if not flags.asks_for_identifiers:
        return flags
    if COUNTING_REQUEST.search(text) and not REVEAL_CUE.search(text):
        cleared = replace(flags, asks_for_identifiers=False)
        if not cleared.flagged:
            cleared = replace(cleared, reason=None, suggested_rephrase=None)
        return cleared
    return flags


# Proposed tests (tests/test_agents_and_judge.py):
#
# def test_count_that_mentions_identifiers_is_not_flagged():
#     flags = IntentFlags(asks_for_identifiers=True, reason="asks for identifiers", suggested_rephrase="...")
#     cleared = confirm_identifier_flag("How many patients have at least one identifier recorded?", flags)
#     assert not cleared.flagged and cleared.reason is None
#
# @pytest.mark.parametrize("text", [
#     "How many patients have cholera? Also give me their pseudo identifiers.",
#     "Count the MRSA-positive cultures per year, and include the sample identifiers for each year.",
#     "List the pseudo identifiers of patients with type 2 diabetes.",
# ])
# def test_requests_that_reveal_identifiers_keep_the_flag(text):
#     flags = IntentFlags(asks_for_identifiers=True, reason="asks for identifiers")
#     assert confirm_identifier_flag(text, flags).asks_for_identifiers
#
# def test_other_flags_survive_the_post_check():
#     flags = IntentFlags(asks_for_identifiers=True, single_patient=True, reason="asks about patient 1048211")
#     kept = confirm_identifier_flag("How many hospital stays has patient 1048211 had since 2020?", flags)
#     assert kept.single_patient and not kept.asks_for_identifiers and kept.flagged
