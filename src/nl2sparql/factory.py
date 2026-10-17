"""Wires the pipeline from settings: data files, agent backends, guard."""
from __future__ import annotations

from nl2sparql.agents import ExampleQueryAgent, HeuristicIntentAgent, LLMIntentAgent, LLMQueryAgent
from nl2sparql.agents.prompts import intent_prompt, query_prompt
from nl2sparql.config import AgentSettings, Settings
from nl2sparql.context import ContextBuilder, ExampleBank, SchemaCards, TerminologyIndex
from nl2sparql.guard import ComplianceGuard, PropertyKinds
from nl2sparql.judge import Judge
from nl2sparql.llm import LLMClient
from nl2sparql.orchestrator import Pipeline


def llm_client(agent: AgentSettings) -> LLMClient:
    return LLMClient(agent.base_url, agent.api_key, agent.timeout_seconds,
                     verify_tls=agent.verify_tls, extra_body=agent.extra_body)


def build_pipeline(settings: Settings, exclude_exact_examples: bool = False) -> Pipeline:
    schema, property_kinds = _load_profile_snapshots(settings.data_dir, settings.assets_dir / "value_sets.json")
    examples_path = settings.data_dir / "examples.jsonl"
    terminology_path = settings.assets_dir / "terminology_index.jsonl"
    try:
        examples = ExampleBank.load(examples_path)
    except (OSError, TypeError, ValueError) as error:
        raise ValueError(f"Could not load context file {examples_path}: {error}") from error
    try:
        terminology = TerminologyIndex.load(terminology_path)
    except (OSError, TypeError, ValueError) as error:
        raise ValueError(f"Could not load context file {terminology_path}: {error}") from error
    context_builder = ContextBuilder(
        examples,
        schema,
        terminology,
        k_examples=settings.context_examples,
        k_cards=settings.context_cards,
        k_codes=settings.context_codes,
        exclude_exact_examples=exclude_exact_examples,
    )
    if settings.backend == "llm":
        intent, query = settings.agent("intent"), settings.agent("query")
        if not query.model:
            raise ValueError("NL2SPARQL_BACKEND=llm needs LLM_QUERY_MODEL (and optionally LLM_INTENT_MODEL).")
        intent_agent = LLMIntentAgent(llm_client(intent), intent.model, intent.max_tokens, intent_prompt(intent.prompt_version),
                                      fallback=HeuristicIntentAgent())
        query_agent = LLMQueryAgent(llm_client(query), query.model, query.max_tokens, query_prompt(query.prompt_version),
                                    reorder_patterns=settings.query_reorder)
    elif settings.backend == "stub":
        intent_agent, query_agent = HeuristicIntentAgent(), ExampleQueryAgent()
    else:
        raise ValueError(f"Unknown NL2SPARQL_BACKEND {settings.backend!r}: use 'stub' or 'llm'.")
    return Pipeline(
        intent_agent=intent_agent,
        query_agent=query_agent,
        context_builder=context_builder,
        judge=Judge(schema),
        guard=ComplianceGuard(property_kinds),
        max_input_chars=settings.max_input_chars,
        max_retries=settings.max_retries,
        retry_with_previous_query=settings.retry_with_previous_query,
        log_path=settings.log_path,
        request_timeout_seconds=settings.request_timeout_seconds,
    )


def _load_profile_snapshots(data_dir, value_sets_path=None):
    schema_path = data_dir / "schema_cards.json"
    kinds_path = data_dir / "property_kinds.json"
    missing = [path.name for path in (schema_path, kinds_path) if not path.is_file()]
    if missing:
        raise ValueError(
            f"Missing RDF profile snapshot(s) in {data_dir}: {', '.join(missing)}. "
            "Rebuild them with scripts/profile_data.py before starting the pipeline."
        )
    try:
        schema = SchemaCards.load(schema_path, value_sets_path)  # value sets: optional local asset
        kinds = PropertyKinds.load(kinds_path)
    except (OSError, TypeError, ValueError, AttributeError, KeyError) as error:
        raise ValueError(
            f"Could not load RDF profile snapshots from {data_dir}: {error}. "
            "Rebuild schema_cards.json and property_kinds.json with scripts/profile_data.py."
        ) from error

    if (
        not isinstance(schema.classes, dict)
        or not schema.classes
        or not schema.data_terms
        or not schema.schema_terms
        or any(
            not isinstance(card, dict)
            or not isinstance(card.get("properties"), dict)
            for card in schema.classes.values()
        )
    ):
        raise ValueError(
            f"RDF profile snapshot {schema_path} is empty or incomplete. "
            "Rebuild it with scripts/profile_data.py before starting the pipeline."
        )
    if not isinstance(kinds.kinds, dict) or not kinds.kinds:
        raise ValueError(
            f"RDF profile snapshot {kinds_path} is empty or incomplete. "
            "Rebuild it with scripts/profile_data.py before starting the pipeline."
        )

    valid_kinds = {"literal", "individual", "resource", "mixed", "unknown"}
    for iri, info in kinds.kinds.items():
        if (
            not isinstance(iri, str)
            or not isinstance(info, dict)
            or info.get("kind") not in valid_kinds
            or not isinstance(info.get("count"), int)
            or info["count"] < 0
        ):
            raise ValueError(
                f"RDF profile snapshot {kinds_path} has an invalid entry for {iri!r}. "
                "Rebuild it with scripts/profile_data.py."
            )
    return schema, kinds
