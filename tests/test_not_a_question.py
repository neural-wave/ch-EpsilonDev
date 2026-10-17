import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline_and_api import EXAMPLES, FixedIntentAgent, ScriptedQueryAgent, pipeline_with, settings  # noqa: E402

from nl2sparql.api import create_app  # noqa: E402
from nl2sparql.config import Settings  # noqa: E402
from nl2sparql.models import IntentFlags  # noqa: E402
from nl2sparql.not_a_question import NOT_A_QUESTION_REASON, is_small_talk, reads_no_data  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
WEATHER = 'PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nSELECT ?dummy WHERE { BIND("Weather data is not available." AS ?dummy) }'


@pytest.mark.parametrize("text", [
    "hey", "Hey!", "heyyy", "hello there", "hi team", "Good morning", "good evening everyone", "hola",
    "thanks", "Thank you so much!", "thx", "ok", "okay", "cool", "yes", "no", "bye", "see you later",
    "how are you?", "hey, how are you?", "Hello! How are you doing today?", "what can you do?", "help",
    "who are you", "test", "testing", "ping", "what's up", "???", "123", "...", "👋", "  hola  ",
])
def test_small_talk_is_not_a_question(text):
    assert is_small_talk(text)


@pytest.mark.parametrize("text", [
    "How many patients have cholera?", "hello, how many patients have cholera?", "Hi! Patients by sex?",
    "test results by sex", "patients by sex", "cholera", "asthma", "How many?", "Which patients have cholera?",
    "thanks, and now split it by year", "ok so how many diagnoses per year", "no cholera patients by age",
    "help me count the patients with diabetes", "What is the average BMI?", "COVID-19 cases in 2021",
])
def test_data_questions_are_not_small_talk(text):
    assert not is_small_talk(text)


def _questions() -> list[str]:
    # The dev split and the official examples; the holdout rows are a blind exam and are never read here.
    texts = [json.loads(line)["question"] for line in (REPO / "data" / "examples.jsonl").read_text().splitlines() if line.strip()]
    for line in (REPO / "eval" / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip() and '"split": "dev"' in line:
            texts.append(json.loads(line)["question"])
    return texts


def test_no_real_question_is_small_talk():
    texts = _questions()
    assert len(texts) > 60 and [text for text in texts if is_small_talk(text)] == []


def _valid_queries() -> list[str]:
    queries = [json.loads(line)["sparql"] for name in ("examples.jsonl", "team_examples.jsonl")
               for line in (REPO / "data" / name).read_text().splitlines() if line.strip()]
    for line in (REPO / "eval" / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip() and '"split": "dev"' in line:
            queries.append(json.loads(line)["reference_sparql"])
    return queries


@pytest.mark.parametrize("sparql", [
    WEATHER,
    'SELECT ?message WHERE { BIND("I am an AI assistant." AS ?message) }',
    'SELECT ?x WHERE { VALUES ?x { "asdfgh" } }',
    "SELECT (COUNT(*) AS ?n) WHERE { }",
])
def test_a_query_that_reads_no_data(sparql):
    assert reads_no_data(sparql)


@pytest.mark.parametrize("sparql", [
    "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s a <https://biomedit.ch/rdf/sphn-schema/sphn#Death> } }",
    "SELECT ?n WHERE { { SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o } } }",
    "SELECT ?x WHERE { OPTIONAL { ?x <urn:p>/<urn:q> ?y } BIND(1 AS ?z) }",
    "SELECT ?g WHERE { GRAPH ?g { ?s a ?c } } LIMIT 1",
    "this is not SPARQL",  # unparseable: the judge handles it, this check does not
    # A schema question (allowed, GUARD.md section 6): the model lists schema terms without reading patients.
    "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nSELECT (COUNT(?prop) AS ?n) WHERE { VALUES ?prop { sphn:hasCode sphn:hasQuantity } }",
])
def test_a_query_that_reads_data_or_does_not_parse(sparql):
    assert not reads_no_data(sparql)


def test_every_reference_and_example_query_reads_data():
    queries = _valid_queries()
    assert len(queries) > 60 and [query for query in queries if reads_no_data(query)] == []


class CountingIntentAgent(FixedIntentAgent):
    calls = 0

    async def classify(self, text):
        CountingIntentAgent.calls += 1
        return await super().classify(text)


def test_small_talk_is_answered_before_any_model_call():
    agent, intent = ScriptedQueryAgent(), CountingIntentAgent()
    CountingIntentAgent.calls = 0
    result = asyncio.run(pipeline_with(agent, intent).run("Hey, how are you?"))
    assert (result.sparql_query, result.is_compliant, result.compliance_reason) == ("", False, NOT_A_QUESTION_REASON)
    assert result.tokens_consumed == 0 and agent.feedback == [] and CountingIntentAgent.calls == 0
    assert result.diagnostics["not_a_question"] == "small_talk" and result.suggested_rephrase is None


def test_a_query_that_reads_no_data_is_answered_without_a_retry():
    agent = ScriptedQueryAgent(WEATHER)
    result = asyncio.run(pipeline_with(agent).run("What is the weather in Zurich?"))
    assert (result.sparql_query, result.is_compliant, result.compliance_reason) == ("", False, NOT_A_QUESTION_REASON)
    assert agent.feedback == [None] and result.retries == 0  # one call, no judge or guard retry
    assert result.diagnostics["not_a_question"] == "query_reads_no_data"
    assert result.diagnostics["not_a_question_sparql"].startswith("PREFIX sphn:")
    assert result.tokens_consumed == 60  # the intent call and the one query call


def test_a_flagged_request_keeps_its_own_reason_when_the_query_reads_no_data():
    flags = IntentFlags(asks_for_identifiers=True, reason="Asks for patient identifiers.")
    result = asyncio.run(pipeline_with(ScriptedQueryAgent(WEATHER), FixedIntentAgent(flags)).run("List the patient ids"))
    assert not result.is_compliant and result.compliance_reason != NOT_A_QUESTION_REASON
    assert "not_a_question" not in result.diagnostics


def test_a_real_question_is_unchanged():
    result = asyncio.run(pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"])).run("Cholera by sex?"))
    assert result.is_compliant and result.sparql_query and "not_a_question" not in result.diagnostics


def test_the_judged_endpoint_answers_small_talk_with_the_fixed_fields():
    client = TestClient(create_app(pipeline=pipeline_with(ScriptedQueryAgent()), settings=settings()))
    assert client.post("/nl-to-sparql", json={"text": "hey"}).json() == {
        "sparql_query": "", "tokens_consumed": 0, "is_compliant": False, "compliance_reason": NOT_A_QUESTION_REASON}


@pytest.mark.parametrize("details", [True, False])
def test_the_demo_marks_a_request_that_is_not_a_question(details):
    app = create_app(pipeline=pipeline_with(ScriptedQueryAgent()), settings=Settings(log_path=None, demo_show_details=details))
    answer = TestClient(app).post("/demo/ask", json={"text": "hey"}).json()
    assert answer["not_a_question"] is True and answer["is_compliant"] is False
    assert answer["compliance_reason"] == NOT_A_QUESTION_REASON and answer["sparql_query"] is None
    assert not answer["executed"] and answer["follow_ups"] == []
    real_app = create_app(pipeline=pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"])),
                          settings=Settings(log_path=None, demo_show_details=details))
    real = TestClient(real_app).post("/demo/ask", json={"text": "Cholera by sex?"}).json()
    assert real["not_a_question"] is False and real["is_compliant"]
