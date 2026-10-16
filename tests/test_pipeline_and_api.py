import asyncio

import pytest
from fastapi.testclient import TestClient

from conftest import DATA_DIR, load_examples
from nl2sparql.api import create_app
from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.agents.intent_llm import IntentReply
from nl2sparql.agents.query_llm import QueryReply
from nl2sparql.models import IntentFlags
from nl2sparql.orchestrator import CUT_OFF_FEEDBACK

EXAMPLES = {e["id"]: e["sparql"] for e in load_examples()}


def settings(**overrides) -> Settings:
    return Settings(data_dir=DATA_DIR, log_path=None, **overrides)


class ScriptedQueryAgent:
    """Returns queries from a list, one per call, and records the feedback it got."""

    def __init__(self, *queries):
        self.queries = list(queries)
        self.feedback = []

    async def generate(self, text, context, feedback=None):
        self.feedback.append(feedback)
        return self.queries.pop(0), 50


class FixedIntentAgent:
    def __init__(self, flags=IntentFlags()):
        self.flags = flags

    async def classify(self, text):
        await asyncio.sleep(0)
        return self.flags, 10


class DetailedQueryAgent(ScriptedQueryAgent):
    """Like an LLM query agent: also says whether each answer was cut off at max_tokens."""

    def __init__(self, *replies):
        super().__init__(*(sparql for sparql, _ in replies))
        self.finish_reasons = [finish for _, finish in replies]

    async def generate_detailed(self, text, context, feedback=None):
        sparql, used = await self.generate(text, context, feedback)
        return QueryReply(sparql, used, sparql, self.finish_reasons.pop(0))


class BrokenJsonIntentAgent:
    async def classify(self, text):
        return (await self.classify_detailed(text)).flags, 10

    async def classify_detailed(self, text):
        return IntentReply(IntentFlags(), 10, '{"asks_for', False, "length")


def pipeline_with(query_agent, intent_agent=None):
    pipeline = build_pipeline(settings())
    pipeline.query_agent = query_agent
    pipeline.intent_agent = intent_agent or FixedIntentAgent()
    return pipeline


def test_compliant_request_end_to_end():
    result = asyncio.run(pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"])).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 0 and result.tokens_consumed == 60
    assert {"intent", "context", "generate", "guard", "total"} <= set(result.timings_ms)
    assert result.tokens_by_step == {"intent": 10, "query": 50} and result.diagnostics == {}


def test_allowed_request_with_leaking_query_is_retried_once():
    agent = ScriptedQueryAgent(EXAMPLES["official-12"], EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent).run("Cholera cases by sex?"))
    assert result.is_compliant and result.retries == 1 and result.tokens_consumed == 110
    assert agent.feedback[1].startswith("The previous query breaks the privacy rules")
    assert result.tokens_by_step == {"intent": 10, "query": 50, "query_retry": 50}
    assert result.diagnostics["retry_reason"] == "guard" and result.diagnostics["guard_violations_first_try"]


def test_forbidden_request_is_never_retried():
    agent = ScriptedQueryAgent(EXAMPLES["official-15"])
    flags = IntentFlags(asks_for_identifiers=True, reason="asks which patients have cholera")
    result = asyncio.run(pipeline_with(agent, FixedIntentAgent(flags)).run("Which patients have cholera?"))
    assert not result.is_compliant and result.retries == 0
    assert result.compliance_reason.startswith("The request asks which patients have cholera.")


def test_judge_error_uses_the_retry():
    agent = ScriptedQueryAgent("SELECT ?x WHERE {", EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 1 and agent.feedback[1].startswith("Syntax error")
    assert result.diagnostics["retry_reason"] == "judge"
    assert result.diagnostics["judge_errors_first_try"][0].startswith("Syntax error")
    assert result.tokens_consumed == sum(result.tokens_by_step.values())


def test_cut_off_answer_is_recorded_and_the_retry_is_told_to_be_shorter():
    agent = DetailedQueryAgent(("```sparql\nSELECT ?x WHERE {", "length"), (EXAMPLES["official-03"], "stop"))
    result = asyncio.run(pipeline_with(agent).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 1
    assert agent.feedback[1].startswith(CUT_OFF_FEEDBACK) and "Syntax error" in agent.feedback[1]
    assert result.diagnostics["cut_off"] == ["query"] and result.diagnostics["retry_reason"] == "judge"


def test_retry_after_a_complete_answer_gets_no_cut_off_note():
    agent = DetailedQueryAgent(("SELECT ?x WHERE {", "stop"), (EXAMPLES["official-03"], "stop"))
    result = asyncio.run(pipeline_with(agent).run("Cholera by sex?"))
    assert agent.feedback[1].startswith("Syntax error") and "cut_off" not in result.diagnostics


def test_unusable_intent_answer_is_recorded():
    agent = ScriptedQueryAgent(EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent, BrokenJsonIntentAgent()).run("Cholera by sex?"))
    assert result.diagnostics["intent_invalid_json"] is True and result.diagnostics["cut_off"] == ["intent"]
    assert result.tokens_by_step["intent"] == 10


@pytest.fixture()
def client():
    return TestClient(create_app(settings=settings()))


def test_api_compliant_response_matches_the_spec(client):
    response = client.post("/nl-to-sparql", json={"text": "How many patients have cholera, by sex?"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"sparql_query", "tokens_consumed", "is_compliant"}
    assert body["is_compliant"] is True and isinstance(body["tokens_consumed"], int)


def test_api_non_compliant_response_has_a_reason(client):
    response = client.post("/nl-to-sparql", json={"text": "How many patients have cholera, and which ones are they?"})
    body = response.json()
    assert response.status_code == 200 and body["is_compliant"] is False
    assert body["compliance_reason"] and body["sparql_query"]


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "   "}, {}, {"text": 42}])
def test_api_rejects_bad_input_with_an_error_field(client, payload):
    response = client.post("/nl-to-sparql", json=payload)
    assert response.status_code == 400 and "error" in response.json()


def test_api_can_hide_the_query_of_forbidden_requests():
    client = TestClient(create_app(settings=settings(return_noncompliant_query=False)))
    body = client.post("/nl-to-sparql", json={"text": "How many patients have cholera, and which ones are they?"}).json()
    assert body["is_compliant"] is False and body["sparql_query"] == "" and body["compliance_reason"]


def test_demo_never_shows_the_query_of_forbidden_requests(client):
    body = client.post("/demo/ask", json={"text": "How many patients have cholera, and which ones are they?"}).json()
    assert body["is_compliant"] is False and body["sparql_query"] is None and body["executed"] is False


def test_health_and_demo_page(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "NL-to-SPARQL" in client.get("/").text
