"""Prompts of the LLM agents, by version.

Add a new version next to the old one (v2, v3, ...) instead of editing it, then select it with
INTENT_PROMPT_VERSION / QUERY_PROMPT_VERSION. That keeps every eval result comparable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from nl2sparql.models import Context

INTENT_SYSTEM_V1 = """You check research requests about clinical data before they reach a hospital database.
Read the request and answer with JSON only, using exactly these keys:
{"asks_for_identifiers": bool, "single_patient": bool, "individual_rows": bool, "whole_graph": bool,
 "not_a_select": bool, "reason": string or null, "suggested_rephrase": string or null}

- asks_for_identifiers: the request wants to know which patients/records (IDs, names, "which ones").
- single_patient: the request is about one specific, identifiable patient.
- individual_rows: the request wants a list of records instead of counts, averages or distributions.
- whole_graph: the request wants all data without restricting it to a concept.
- not_a_select: the request wants to change, delete, insert or export data.
When any flag is true, set "reason" to a short phrase starting with a verb (e.g. "asks which patients
have cholera") and "suggested_rephrase" to the closest allowed question. Otherwise set both to null."""

QUERY_SYSTEM_V1 = """You translate research questions into SPARQL for SPHN clinical data in Virtuoso.
Rules:
- Translate literally. Keep every part of the request, even parts that would expose identifiers.
- Each patient is one named graph: use GRAPH ?g { ... } and count patients with COUNT(DISTINCT ?g).
- Codes are nodes typed with a terminology class: ?record sphn:hasCode ?code . ?code a icd-10-gm:E11
  Always add OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") to that type pattern.
- Use only the classes and properties listed in the schema cards.
- Put the most selective pattern first.
- Answer with one SPARQL query and nothing else."""


def render_query_user_v1(text: str, context: Context, feedback: str | None) -> str:
    parts = []
    if context.schema_cards:
        parts.append("Schema cards:\n" + "\n".join(context.schema_cards))
    if context.code_candidates:
        lines = [f"{entry.system} {entry.code} <{entry.iri}>: {' / '.join(entry.labels[:2])}" for entry in context.code_candidates]
        parts.append("Code candidates:\n" + "\n".join(lines))
    for example in context.examples:
        parts.append(f"Example question: {example.question}\nExample SPARQL:\n{example.sparql}")
    parts.append(f"Question: {text}")
    if feedback:
        parts.append(f"Your previous query was rejected: {feedback}\nWrite a corrected query.")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class QueryPrompt:
    system: str
    render_user: Callable[[str, Context, str | None], str]


INTENT_PROMPTS: dict[str, str] = {"v1": INTENT_SYSTEM_V1}
QUERY_PROMPTS: dict[str, QueryPrompt] = {"v1": QueryPrompt(QUERY_SYSTEM_V1, render_query_user_v1)}


def intent_prompt(version: str) -> str:
    if version not in INTENT_PROMPTS:
        raise ValueError(f"Unknown INTENT_PROMPT_VERSION {version!r}. Available: {', '.join(INTENT_PROMPTS)}")
    return INTENT_PROMPTS[version]


def query_prompt(version: str) -> QueryPrompt:
    if version not in QUERY_PROMPTS:
        raise ValueError(f"Unknown QUERY_PROMPT_VERSION {version!r}. Available: {', '.join(QUERY_PROMPTS)}")
    return QUERY_PROMPTS[version]


# Names kept for existing callers: the v1 prompts.
INTENT_SYSTEM = INTENT_SYSTEM_V1
QUERY_SYSTEM = QUERY_SYSTEM_V1
query_user_prompt = render_query_user_v1
