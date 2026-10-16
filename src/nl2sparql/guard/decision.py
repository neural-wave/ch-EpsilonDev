"""Final verdict: the query check combined with the intent flags (spec section 4.7).

| intent flags | query check | verdict                                             |
|--------------|-------------|-----------------------------------------------------|
| none         | clean       | compliant                                           |
| raised       | broken      | non-compliant, no retry                             |
| raised       | clean       | non-compliant, no retry (the query was cleaned up)  |
| none         | broken      | non-compliant + retry hint (query agent's mistake)  |

A query that breaks a rule is never marked compliant.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nl2sparql.guard.provenance import PropertyKinds
from nl2sparql.guard.rules import RuleSettings, Violation, check_rules
from nl2sparql.models import IntentFlags
from nl2sparql.sparql.parse import parse_sparql

FLAG_RULES = {
    "single_patient": "R1",
    "individual_rows": "R2",
    "whole_graph": "R3",
    "not_a_select": "R4",
    "asks_for_identifiers": "R5",
}
FLAG_MESSAGES = {
    "single_patient": "The request is about a single, identifiable patient.",
    "individual_rows": "The request asks for individual records instead of aggregated results.",
    "whole_graph": "The request asks to match the whole database without restriction.",
    "not_a_select": "The request asks to change or export data; only read-only SELECT queries are allowed.",
    "asks_for_identifiers": "The request asks for patient or record identifiers, which must never be exposed.",
}
RULE_HINTS = {
    "R1": "Ask about all patients, or about a group defined by a condition.",
    "R2": "Ask for a count, an average or a distribution instead of a list of records.",
    "R3": "Narrow the question to a concept, such as a diagnosis, a lab test or a drug.",
    "R4": "Ask a question that reads data; changes and exports are not possible.",
    "R5": "Ask how many patients, not who they are.",
    "PARSE": "Rephrase the question in simpler terms.",
}


@dataclass(frozen=True)
class GuardDecision:
    compliant: bool
    reason: str | None = None
    retry_hint: str | None = None  # set only when the query agent should try again
    suggested_rephrase: str | None = None
    violations: list[Violation] = field(default_factory=list)


class ComplianceGuard:
    def __init__(self, kinds: PropertyKinds, settings: RuleSettings = RuleSettings()):
        self.kinds = kinds
        self.settings = settings

    def check_query(self, sparql: str) -> list[Violation]:
        return check_rules(parse_sparql(sparql), self.kinds, self.settings)

    def decide(self, sparql: str, flags: IntentFlags) -> GuardDecision:
        violations = self.check_query(sparql)
        if not violations and not flags.flagged:
            return GuardDecision(compliant=True)
        hint = flags.suggested_rephrase or _rule_hint(violations, flags)
        retry_hint = None
        if violations and not flags.flagged:
            retry_hint = (
                "The previous query breaks the privacy rules: "
                + " ".join(violation.message for violation in violations)
                + " Rewrite it so that it only returns results aggregated across all patients."
            )
        return GuardDecision(
            compliant=False,
            reason=_reason(violations, flags),
            retry_hint=retry_hint,
            suggested_rephrase=hint,
            violations=violations,
        )


def _rule_hint(violations: list[Violation], flags: IntentFlags) -> str:
    rule = violations[0].rule if violations else FLAG_RULES[flags.raised()[0]]
    return RULE_HINTS.get(rule, RULE_HINTS["R2"])


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _reason(violations: list[Violation], flags: IntentFlags) -> str:
    parts = []
    if flags.reason:
        parts.append(_sentence(f"The request {flags.reason}" if flags.reason[:1].islower() else flags.reason))
    if violations:
        parts += [violation.message for violation in violations]
    else:
        parts += [FLAG_MESSAGES[name] for name in flags.raised()]
    if flags.suggested_rephrase:
        parts.append(f'Try instead: "{flags.suggested_rephrase}"')
    else:
        parts.append(_rule_hint(violations, flags))
    return " ".join(parts)
