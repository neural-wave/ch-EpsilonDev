import asyncio
import json

import httpx
import pytest

from conftest import DATA_DIR
from nl2sparql.agents import ExampleQueryAgent, HeuristicIntentAgent, LLMIntentAgent, LLMQueryAgent
from nl2sparql.context import SchemaCards
from nl2sparql.judge import Judge
from nl2sparql.llm import LLMClient, LLMError
from nl2sparql.models import Context, Example


def run(coroutine):
    return asyncio.run(coroutine)


def test_heuristic_intent_flags_the_official_forbidden_requests():
    agent = HeuristicIntentAgent()
    flags, tokens = run(agent.classify("How many patients have cholera, and which ones are they?"))
    assert flags.asks_for_identifiers and tokens == 0
    assert flags.suggested_rephrase == "How many patients have cholera?"
    assert run(agent.classify("How many lab tests has patient 0042 had?"))[0].single_patient
    assert run(agent.classify("How much data do we have in total?"))[0].whole_graph
    assert not run(agent.classify("How many patients have a billed diagnosis of cholera?"))[0].flagged


def test_example_agent_returns_the_top_example():
    context = Context(examples=[Example("x", "q", "SELECT * WHERE { ?s ?p ?o }", True)])
    assert run(ExampleQueryAgent().generate("q", context)) == ("SELECT * WHERE { ?s ?p ?o }", 0)


def fake_server(reply_text: str, seen: list, finish_reason: str = "stop"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": reply_text}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })
    return httpx.MockTransport(handler)


def test_llm_intent_agent_parses_json_and_counts_tokens():
    seen = []
    reply = '{"asks_for_identifiers": true, "reason": "asks which patients", "suggested_rephrase": "How many?"}'
    agent = LLMIntentAgent(LLMClient("http://gpu/v1", transport=fake_server(reply, seen)), "small-model")
    flags, tokens = run(agent.classify("Which patients have cholera?"))
    assert flags.asks_for_identifiers and not flags.single_patient and tokens == 120
    assert seen[0]["model"] == "small-model" and seen[0]["response_format"] == {"type": "json_object"}


def test_llm_client_merges_extra_body():
    seen = []
    client = LLMClient("http://gpu/v1", transport=fake_server("{}", seen),
                       extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    run(LLMIntentAgent(client, "m").classify("hi"))
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False} and seen[0]["model"] == "m"


def test_llm_query_agent_extracts_sparql_from_a_fenced_answer():
    seen = []
    agent = LLMQueryAgent(LLMClient("http://gpu/v1", transport=fake_server("Here:\n```sparql\nSELECT ?x WHERE { ?x ?p ?o }\n```", seen)), "coder")
    sparql, tokens = run(agent.generate("q", Context(), feedback="add an aggregate"))
    assert sparql == "SELECT ?x WHERE { ?x ?p ?o }\n" and tokens == 120
    assert "previous query was rejected: add an aggregate" in seen[0]["messages"][1]["content"]


def test_agents_report_answers_cut_off_at_max_tokens():
    seen = []
    query_agent = LLMQueryAgent(LLMClient("http://gpu/v1", transport=fake_server("```sparql\nSELECT ?x WHERE {", seen, "length")), "coder")
    reply = run(query_agent.generate_detailed("q", Context()))
    assert reply.truncated and reply.finish_reason == "length" and reply.tokens == 120
    intent_agent = LLMIntentAgent(LLMClient("http://gpu/v1", transport=fake_server('{"asks_for_identi', seen, "length")), "m")
    intent = run(intent_agent.classify_detailed("Which patients?"))
    assert intent.truncated and not intent.valid_json and not intent.flags.flagged


def failing_server():
    return httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "model server down"}))


def test_intent_agent_falls_back_to_keyword_rules_when_the_model_fails():
    agent = LLMIntentAgent(LLMClient("http://gpu/v1", transport=failing_server()), "m", fallback=HeuristicIntentAgent())
    reply = run(agent.classify_detailed("How many patients have cholera, and which ones are they?"))
    assert reply.fallback == "error" and reply.flags.asks_for_identifiers
    assert reply.tokens == 0 and reply.valid_json is None


def test_intent_agent_falls_back_on_an_unusable_answer_but_counts_its_tokens():
    seen = []
    client = LLMClient("http://gpu/v1", transport=fake_server('{"asks_for_identi', seen, "length"))
    reply = run(LLMIntentAgent(client, "m", fallback=HeuristicIntentAgent()).classify_detailed("Which patients have cholera?"))
    assert reply.fallback == "cut_off" and reply.flags.asks_for_identifiers and reply.tokens == 120


def test_intent_agent_without_fallback_still_raises():
    agent = LLMIntentAgent(LLMClient("http://gpu/v1", transport=failing_server()), "m")
    with pytest.raises(LLMError):
        run(agent.classify("Which patients have cholera?"))


def test_judge_fixes_prefixes_and_flags_terms_missing_from_the_data():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    ok = judge.check("SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasRank ?r } }")
    assert ok.ok and ok.sparql.startswith("PREFIX sphn:")
    wrong = judge.check("SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d sphn:hasRankCode ?r } }")
    assert wrong.errors == ["sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?"]
    assert judge.check("SELECT ?x WHERE {").errors[0].startswith("Syntax error")
