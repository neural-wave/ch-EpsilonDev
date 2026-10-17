"""Final verdict: the query check combined with the intent flags (spec section 4.7).

| intent flags | query check        | verdict                                                    |
|--------------|--------------------|------------------------------------------------------------|
| none         | clean              | compliant                                                  |
| raised       | broken             | non-compliant, no retry                                    |
| raised       | clean              | non-compliant, no retry (the query was cleaned up)         |
| none         | broken, R1 among   | non-compliant, no retry (the request named the patient)    |
| none         | broken, no R1      | non-compliant + retry hint (query agent's mistake)         |

A query that breaks a rule is never marked compliant. An R1 violation is never retried: the model cannot
invent a graph name, a record IRI or identifier digits, so they came from the request, and a retry would
only let the model drop the forbidden part and hand back a clean query for a forbidden request.

The retry hint repeats the violations and closes with the sentence they ask for (Violation.advice, see
rules.py). A query rdflib cannot parse gets the judge's diagnosis of the text instead (judge_checks/syntax.py,
rdflib's own message when no known shape matches): on the 17 October runs none of the six parse-error
retries that were told "breaks the privacy rules ... aggregated across all patients" came back parseable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nl2sparql.guard.provenance import PropertyKinds
from nl2sparql.guard.rules import (  # the advice sentences are re-exported here for the tests and the docs
    ADVICE_PRIORITY, AGGREGATION_ADVICE, GROUPING_ADVICE, TOTALS_ADVICE, RuleSettings, Violation, check_rules,
)
from nl2sparql.judge_checks.syntax import diagnose
from nl2sparql.models import IntentFlags
from nl2sparql.sparql.parse import parse_sparql

__all__ = [
    "AGGREGATION_ADVICE", "ComplianceGuard", "GROUPING_ADVICE", "GuardDecision", "REQUEST_LEVEL_RULES",
    "SYNTAX_LEAD", "TOTALS_ADVICE",
]

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
REQUEST_LEVEL_RULES = {"R1"}  # the query names a patient, a record or an identifier: that came from the request
SYNTAX_LEAD = "The previous query is not valid SPARQL."


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
        parsed = parse_sparql(sparql)
        violations = check_rules(parsed, self.kinds, self.settings)
        if not violations and not flags.flagged:
            return GuardDecision(compliant=True)
        hint = flags.suggested_rephrase or _rule_hint(violations, flags)
        retry_hint = None
        request_level = any(violation.rule in REQUEST_LEVEL_RULES for violation in violations)
        if violations and not flags.flagged and not request_level:
            retry_hint = _syntax_hint(sparql, parsed.error) if parsed.kind == "Invalid" else _rules_hint(violations)
        return GuardDecision(
            compliant=False,
            reason=_reason(violations, flags),
            retry_hint=retry_hint,
            suggested_rephrase=hint,
            violations=violations,
        )


def _rules_hint(violations: list[Violation]) -> str:
    """Every violation, then the closing sentence the violations ask for; ADVICE_PRIORITY settles disagreements."""
    wanted = {violation.advice for violation in violations}
    advice = next((candidate for candidate in ADVICE_PRIORITY if candidate in wanted), AGGREGATION_ADVICE)
    return "The previous query breaks the privacy rules: " + " ".join(violation.message for violation in violations) + " " + advice


def _syntax_hint(sparql: str, error: str | None) -> str:
    """The judge's diagnosis of the same text, so the retry hears about the mistake and not about privacy."""
    hints = diagnose(sparql) or [error or "rdflib could not parse it."]
    return " ".join([SYNTAX_LEAD, *hints])


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
