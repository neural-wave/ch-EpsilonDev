import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import DATA_DIR, load_examples
from nl2sparql.api import create_app
from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.agents.intent_llm import IntentReply
from nl2sparql.agents.query_llm import QueryReply
from nl2sparql.models import IntentFlags
from nl2sparql.orchestrator import CUT_OFF_FEEDBACK, PipelineError

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
    assert result.tokens_by_step == {"intent": 10, "query": 50} and result.diagnostics == {}


def test_intent_runs_while_context_is_built():
    context_started = threading.Event()
    release_context = threading.Event()

    class WaitingContextBuilder:
        def __init__(self, delegate):
            self.delegate = delegate

        def build(self, text):
            context_started.set()
            assert release_context.wait(timeout=5), "Intent did not run during context building."
            return self.delegate.build(text)

    class ReleasingIntentAgent:
        async def classify(self, text):
            assert await asyncio.to_thread(context_started.wait, 5)
            release_context.set()
            return IntentFlags(), 10

    pipeline = pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"]), ReleasingIntentAgent())
    pipeline.context_builder = WaitingContextBuilder(pipeline.context_builder)
    result = asyncio.run(pipeline.run("Cholera by sex?"))
    assert result.is_compliant and result.tokens_consumed == 60


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


def test_forbidden_request_with_judge_error_is_never_retried():
    agent = ScriptedQueryAgent("SELECT ?x WHERE {", EXAMPLES["official-03"])
    flags = IntentFlags(asks_for_identifiers=True, reason="asks which patients have cholera")
    result = asyncio.run(pipeline_with(agent, FixedIntentAgent(flags)).run("Which patients have cholera?"))
    assert not result.is_compliant and result.retries == 0 and result.tokens_consumed == 60
    assert agent.feedback == [None]


def test_forbidden_intent_overrides_an_apparently_safe_query():
    agent = ScriptedQueryAgent(EXAMPLES["official-03"])
    flags = IntentFlags(asks_for_identifiers=True, reason="asks which patients have cholera")
    result = asyncio.run(pipeline_with(agent, FixedIntentAgent(flags)).run("Which patients have cholera?"))
    assert not result.is_compliant and result.retries == 0
    assert result.compliance_reason.startswith("The request asks which patients have cholera.")
    assert agent.feedback == [None]


def test_judge_error_uses_the_retry():
    agent = ScriptedQueryAgent("SELECT ?x WHERE {", EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 1 and result.tokens_consumed == 110
    assert agent.feedback[1].startswith("Syntax error")
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


def test_keyword_fallback_keeps_the_request_alive_when_the_intent_model_fails():
    import httpx
    from nl2sparql.agents import HeuristicIntentAgent, LLMIntentAgent
    from nl2sparql.llm import LLMClient

    down = LLMClient("http://gpu/v1", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    intent_agent = LLMIntentAgent(down, "m", fallback=HeuristicIntentAgent())
    agent = ScriptedQueryAgent(EXAMPLES["official-15"])
    result = asyncio.run(pipeline_with(agent, intent_agent).run("How many patients have cholera, and which ones are they?"))
    assert not result.is_compliant and result.intent.asks_for_identifiers
    assert result.diagnostics["intent_fallback"] == "error" and "intent_invalid_json" not in result.diagnostics


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


def test_request_timeout_is_configured_from_the_environment(monkeypatch):
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "12.5")
    configured = Settings.from_env()
    assert configured.request_timeout_seconds == 12.5
    assert build_pipeline(configured).request_timeout_seconds == 12.5


@pytest.mark.parametrize("invalid", [0, -1, float("inf"), float("nan")])
def test_request_timeout_must_be_positive_and_finite(invalid):
    with pytest.raises(ValueError, match="REQUEST_TIMEOUT_SECONDS"):
        build_pipeline(settings(request_timeout_seconds=invalid))


def test_api_request_timeout_returns_503_and_cancels_agents():
    class WaitingIntentAgent:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def classify(self, text):
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class WaitingQueryAgent:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def generate(self, text, context, feedback=None):
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    configured = settings(request_timeout_seconds=0.2)
    pipeline = build_pipeline(configured)
    intent_agent, query_agent = WaitingIntentAgent(), WaitingQueryAgent()
    pipeline.intent_agent = intent_agent
    pipeline.query_agent = query_agent
    response = TestClient(create_app(pipeline=pipeline, settings=configured)).post(
        "/nl-to-sparql", json={"text": "How many patients have cholera?"}
    )
    assert response.status_code == 503
    assert response.json() == {"error": "Request timed out after 0.2 seconds."}
    assert intent_agent.started and intent_agent.cancelled
    assert query_agent.started and query_agent.cancelled


def test_request_timeout_covers_synchronous_guard_work():
    class SlowGuard:
        def __init__(self, delegate):
            self.delegate = delegate
            self.called = False

        def decide(self, sparql, flags):
            self.called = True
            time.sleep(0.3)
            return self.delegate.decide(sparql, flags)

    configured = settings(request_timeout_seconds=0.2)
    pipeline = build_pipeline(configured)
    pipeline.query_agent = ScriptedQueryAgent(EXAMPLES["official-03"])
    pipeline.intent_agent = FixedIntentAgent()
    slow_guard = SlowGuard(pipeline.guard)
    pipeline.guard = slow_guard
    response = TestClient(create_app(pipeline=pipeline, settings=configured)).post(
        "/nl-to-sparql", json={"text": "How many patients have cholera?"}
    )
    assert slow_guard.called
    assert response.status_code == 503
    assert response.json() == {"error": "Request timed out after 0.2 seconds."}


def test_slow_guard_does_not_hold_the_pipeline_past_its_deadline():
    class SlowGuard:
        def __init__(self):
            self.started = threading.Event()

        def decide(self, sparql, flags):
            self.started.set()
            time.sleep(0.8)

    async def scenario():
        pipeline = pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"]))
        guard = SlowGuard()
        pipeline.guard = guard
        pipeline.request_timeout_seconds = 0.3
        started = time.perf_counter()
        with pytest.raises(PipelineError, match="Request timed out"):
            await pipeline.run("How many patients have cholera?")
        assert guard.started.is_set()
        return time.perf_counter() - started

    assert asyncio.run(scenario()) < 0.6


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


def test_judged_endpoint_does_not_access_virtuoso(monkeypatch):
    def forbidden_client(*args, **kwargs):
        raise AssertionError("The judged endpoint must not access a database.")

    monkeypatch.setattr("nl2sparql.api.VirtuosoClient", forbidden_client)
    client = TestClient(create_app(settings=settings()))
    response = client.post("/nl-to-sparql", json={"text": "How many patients have cholera, by sex?"})
    assert response.status_code == 200 and response.json()["is_compliant"] is True


def test_demo_never_shows_the_query_of_forbidden_requests(client):
    body = client.post("/demo/ask", json={"text": "How many patients have cholera, and which ones are they?"}).json()
    assert body["is_compliant"] is False and body["sparql_query"] is None and body["executed"] is False


def test_health_and_demo_page(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "NL-to-SPARQL" in client.get("/").text
