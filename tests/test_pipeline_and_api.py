import asyncio

import pytest
from fastapi.testclient import TestClient

from conftest import DATA_DIR, load_examples
from nl2sparql.api import create_app
from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.models import IntentFlags

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


class FailingIntentAgent:
    async def classify(self, text):
        await asyncio.sleep(0)
        raise RuntimeError("intent unavailable")


class FailingContextBuilder:
    def build(self, text):
        raise RuntimeError("context unavailable")


class FailingOnCallGuard:
    def __init__(self, delegate, fail_on):
        self.delegate = delegate
        self.fail_on = fail_on
        self.calls = 0

    def decide(self, sparql, flags):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("guard unavailable")
        return self.delegate.decide(sparql, flags)


def pipeline_with(query_agent, intent_agent=None):
    pipeline = build_pipeline(settings())
    pipeline.query_agent = query_agent
    pipeline.intent_agent = intent_agent or FixedIntentAgent()
    return pipeline


def test_compliant_request_end_to_end():
    result = asyncio.run(pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"])).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 0 and result.tokens_consumed == 60
    assert {"intent", "context", "generate", "guard", "total"} <= set(result.timings_ms)


def test_allowed_request_with_leaking_query_is_retried_once():
    agent = ScriptedQueryAgent(EXAMPLES["official-12"], EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent).run("Cholera cases by sex?"))
    assert result.is_compliant and result.retries == 1 and result.tokens_consumed == 110
    assert agent.feedback[1].startswith("The previous query breaks the privacy rules")


def test_forbidden_request_is_never_retried():
    agent = ScriptedQueryAgent(EXAMPLES["official-15"])
    flags = IntentFlags(asks_for_identifiers=True, reason="asks which patients have cholera")
    result = asyncio.run(pipeline_with(agent, FixedIntentAgent(flags)).run("Which patients have cholera?"))
    assert not result.is_compliant and result.retries == 0
    assert result.compliance_reason.startswith("The request asks which patients have cholera.")


def test_forbidden_request_with_judge_error_is_never_retried():
    agent = ScriptedQueryAgent("SELECT ?x WHERE {", EXAMPLES["official-03"])
    flags = IntentFlags(asks_for_identifiers=True, reason="asks which patients have cholera")
    result = asyncio.run(pipeline_with(agent, FixedIntentAgent(flags)).run("Which patients have cholera?"))
    assert not result.is_compliant and result.retries == 0 and result.tokens_consumed == 60
    assert agent.feedback == [None]


def test_judge_error_uses_the_retry():
    agent = ScriptedQueryAgent("SELECT ?x WHERE {", EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 1 and result.tokens_consumed == 110
    assert agent.feedback[1].startswith("Syntax error")


def test_guard_retry_failure_keeps_the_first_query_and_verdict():
    agent = ScriptedQueryAgent(EXAMPLES["official-12"], EXAMPLES["official-03"])
    pipeline = pipeline_with(agent)
    first_query = pipeline.judge.check(EXAMPLES["official-12"]).sparql
    pipeline.guard = FailingOnCallGuard(pipeline.guard, fail_on=2)
    result = asyncio.run(pipeline.run("Cholera cases by sex?"))
    assert not result.is_compliant and result.sparql_query == first_query
    assert result.compliance_reason and result.retries == 1 and result.tokens_consumed == 110


def test_cancelling_a_request_stops_the_intent_agent():
    class WaitingIntentAgent:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def classify(self, text):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    class WaitingQueryAgent:
        def __init__(self):
            self.started = asyncio.Event()

        async def generate(self, text, context, feedback=None):
            self.started.set()
            await asyncio.Event().wait()

    async def scenario():
        intent_agent = WaitingIntentAgent()
        query_agent = WaitingQueryAgent()
        task = asyncio.create_task(pipeline_with(query_agent, intent_agent).run("Cholera by sex?"))
        await asyncio.wait_for(intent_agent.started.wait(), timeout=1)
        await asyncio.wait_for(query_agent.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert intent_agent.cancelled.is_set()

    asyncio.run(scenario())


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


def test_api_returns_503_when_intent_classification_fails():
    pipeline = pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"]), FailingIntentAgent())
    response = TestClient(create_app(pipeline=pipeline, settings=settings())).post(
        "/nl-to-sparql", json={"text": "Which patients have cholera?"}
    )
    assert response.status_code == 503 and set(response.json()) == {"error"}


def test_api_returns_503_when_intent_fails_before_a_judge_retry():
    agent = ScriptedQueryAgent("SELECT ?x WHERE {", EXAMPLES["official-03"])
    pipeline = pipeline_with(agent, FailingIntentAgent())
    response = TestClient(create_app(pipeline=pipeline, settings=settings())).post(
        "/nl-to-sparql", json={"text": "Which patients have cholera?"}
    )
    assert response.status_code == 503 and agent.feedback == [None]


def test_api_returns_503_when_context_building_fails():
    pipeline = pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"]))
    pipeline.context_builder = FailingContextBuilder()
    response = TestClient(create_app(pipeline=pipeline, settings=settings())).post(
        "/nl-to-sparql", json={"text": "How many patients have cholera?"}
    )
    assert response.status_code == 503 and set(response.json()) == {"error"}


def test_api_returns_503_when_guard_fails():
    pipeline = pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"]))
    pipeline.guard = FailingOnCallGuard(pipeline.guard, fail_on=1)
    response = TestClient(create_app(pipeline=pipeline, settings=settings())).post(
        "/nl-to-sparql", json={"text": "How many patients have cholera?"}
    )
    assert response.status_code == 503 and set(response.json()) == {"error"}


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
