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


def pipeline_with(query_agent, intent_agent=None):
    pipeline = build_pipeline(settings())
    pipeline.query_agent = query_agent
    pipeline.intent_agent = intent_agent or FixedIntentAgent()
    return pipeline


def test_compliant_request_end_to_end():
    result = asyncio.run(pipeline_with(ScriptedQueryAgent(EXAMPLES["official-03"])).run("Cholera by sex?"))
    assert result.is_compliant and result.retries == 0 and result.tokens_consumed == 60
    assert {"intent", "context", "generate", "guard", "total"} <= set(result.timings_ms)


@pytest.mark.parametrize(
    "snapshot_name,payload",
    [
        ("schema_cards.json", '{"classes": {}, "schema_terms": []}'),
        ("property_kinds.json", "{}"),
    ],
)
def test_pipeline_rejects_empty_profile_snapshots(tmp_path, snapshot_name, payload):
    (tmp_path / "examples.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "schema_cards.json").write_text(
        '{"classes": {"Example": {"instances": 1, "patients": 1, "properties": {}}}, '
        '"schema_terms": ["Example"]}',
        encoding="utf-8",
    )
    (tmp_path / "property_kinds.json").write_text(
        '{"https://example.test/property": {"kind": "literal", "count": 1}}',
        encoding="utf-8",
    )
    (tmp_path / snapshot_name).write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="RDF profile snapshot"):
        build_pipeline(Settings(data_dir=tmp_path, assets_dir=tmp_path, log_path=None))


def test_pipeline_rejects_missing_profile_snapshot(tmp_path):
    with pytest.raises(ValueError, match="Missing RDF profile snapshot"):
        build_pipeline(Settings(data_dir=tmp_path, assets_dir=tmp_path, log_path=None))


def test_pipeline_rejects_malformed_profile_snapshot(tmp_path):
    (tmp_path / "schema_cards.json").write_text("not json", encoding="utf-8")
    (tmp_path / "property_kinds.json").write_text(
        '{"https://example.test/property": {"kind": "literal", "count": 1}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Could not load RDF profile snapshots"):
        build_pipeline(Settings(data_dir=tmp_path, assets_dir=tmp_path, log_path=None))


@pytest.mark.parametrize("filename,payload", [
    ("examples.jsonl", "{bad json}\n"),
    ("terminology_index.jsonl", '{"iri": "x"}\n'),
])
def test_pipeline_reports_malformed_optional_context_file(tmp_path, filename, payload):
    (tmp_path / "examples.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "schema_cards.json").write_text(
        '{"classes": {"Example": {"instances": 1, "patients": 1, "properties": {}}}, '
        '"schema_terms": ["Example"]}',
        encoding="utf-8",
    )
    (tmp_path / "property_kinds.json").write_text(
        '{"https://example.test/property": {"kind": "literal", "count": 1}}',
        encoding="utf-8",
    )
    if filename == "examples.jsonl":
        (tmp_path / filename).write_text(payload, encoding="utf-8")
        (tmp_path / "terminology_index.jsonl").write_text("", encoding="utf-8")
    else:
        (tmp_path / "examples.jsonl").write_text("", encoding="utf-8")
        (tmp_path / filename).write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match=rf"{tmp_path / filename}"):
        build_pipeline(Settings(data_dir=tmp_path, assets_dir=tmp_path, log_path=None))


def test_allowed_request_with_leaking_query_is_retried_once():
    agent = ScriptedQueryAgent(EXAMPLES["official-12"], EXAMPLES["official-03"])
    result = asyncio.run(pipeline_with(agent).run("Cholera cases by sex?"))
    assert result.is_compliant and result.retries == 1 and result.tokens_consumed == 110
    assert agent.feedback[1].startswith("The previous query breaks the privacy rules")


def test_query_still_violating_after_retry_remains_non_compliant():
    leaking = EXAMPLES["official-12"]
    agent = ScriptedQueryAgent(leaking, leaking)
    result = asyncio.run(pipeline_with(agent).run("Show cholera cases by record?"))
    assert not result.is_compliant and result.retries == 1
    assert result.violations and result.compliance_reason


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
