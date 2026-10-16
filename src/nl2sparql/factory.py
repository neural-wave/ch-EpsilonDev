"""Wires the pipeline from settings: data files, agent backends, guard."""
from __future__ import annotations

from nl2sparql.agents import ExampleQueryAgent, HeuristicIntentAgent, LLMIntentAgent, LLMQueryAgent
from nl2sparql.config import Settings
from nl2sparql.context import ContextBuilder, ExampleBank, SchemaCards, TerminologyIndex
from nl2sparql.guard import ComplianceGuard, PropertyKinds
from nl2sparql.judge import Judge
from nl2sparql.llm import LLMClient
from nl2sparql.orchestrator import Pipeline


def build_pipeline(settings: Settings, exclude_exact_examples: bool = False) -> Pipeline:
    schema = SchemaCards.load(settings.data_dir / "schema_cards.json")
    context_builder = ContextBuilder(
        ExampleBank.load(settings.data_dir / "examples.jsonl"),
        schema,
        TerminologyIndex.load(settings.assets_dir / "terminology_index.jsonl"),
        exclude_exact_examples=exclude_exact_examples,
    )
    if settings.backend == "llm":
        if not settings.llm_query_model:
            raise ValueError("NL2SPARQL_BACKEND=llm needs LLM_QUERY_MODEL (and optionally LLM_INTENT_MODEL).")
        client = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_timeout_seconds,
                           verify_tls=settings.llm_verify_tls, extra_body=settings.llm_extra_body)
        intent_agent = LLMIntentAgent(client, settings.llm_intent_model or settings.llm_query_model)
        query_agent = LLMQueryAgent(client, settings.llm_query_model)
    elif settings.backend == "stub":
        intent_agent, query_agent = HeuristicIntentAgent(), ExampleQueryAgent()
    else:
        raise ValueError(f"Unknown NL2SPARQL_BACKEND {settings.backend!r}: use 'stub' or 'llm'.")
    return Pipeline(
        intent_agent=intent_agent,
        query_agent=query_agent,
        context_builder=context_builder,
        judge=Judge(schema),
        guard=ComplianceGuard(PropertyKinds.load(settings.data_dir / "property_kinds.json")),
        max_input_chars=settings.max_input_chars,
        max_retries=settings.max_retries,
        log_path=settings.log_path,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
