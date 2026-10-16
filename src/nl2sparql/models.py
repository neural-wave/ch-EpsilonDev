"""Types shared by every module. Change them only with the whole team."""
from __future__ import annotations

from dataclasses import dataclass, field

FLAG_NAMES = ("asks_for_identifiers", "single_patient", "individual_rows", "whole_graph", "not_a_select")


@dataclass(frozen=True)
class IntentFlags:
    """What the intent agent read in the request text (not in the query)."""

    asks_for_identifiers: bool = False
    single_patient: bool = False
    individual_rows: bool = False
    whole_graph: bool = False
    not_a_select: bool = False
    reason: str | None = None
    suggested_rephrase: str | None = None

    @property
    def flagged(self) -> bool:
        return any(getattr(self, name) for name in FLAG_NAMES)

    def raised(self) -> list[str]:
        return [name for name in FLAG_NAMES if getattr(self, name)]


@dataclass(frozen=True)
class Example:
    id: str
    question: str
    sparql: str
    compliant: bool
    source: str = "official"


@dataclass(frozen=True)
class CodeEntry:
    iri: str
    code: str
    system: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    examples: list[Example] = field(default_factory=list)
    schema_cards: list[str] = field(default_factory=list)
    code_candidates: list[CodeEntry] = field(default_factory=list)


@dataclass(frozen=True)
class JudgeResult:
    sparql: str
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class PipelineResult:
    sparql_query: str
    tokens_consumed: int
    is_compliant: bool
    compliance_reason: str | None
    suggested_rephrase: str | None
    intent: IntentFlags
    violations: list[str]
    retries: int
    timings_ms: dict[str, float]
    # Added 2026-10-17 (defaults keep older callers working):
    tokens_by_step: dict[str, int] = field(default_factory=dict)  # "intent", "query", "query_retry"
    diagnostics: dict = field(default_factory=dict)  # retry reason and first try, cut-off answers, ...
