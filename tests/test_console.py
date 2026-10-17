import json

import pytest
from fastapi.testclient import TestClient

from conftest import DATA_DIR
from nl2sparql.api import create_app
from nl2sparql.config import Settings


@pytest.fixture()
def client():
    return TestClient(create_app(settings=Settings(data_dir=DATA_DIR, log_path=None, llm_api_key="secret-token-123")))


def test_console_page_loads(client):
    response = client.get("/agents")
    assert response.status_code == 200 and "Agents console" in response.text


def test_config_shows_both_agents_and_hides_secrets(client):
    config = client.get("/agents/config").json()
    assert config["backend"] == "stub"
    intent, query = config["agents"]["intent"], config["agents"]["query"]
    assert intent["implementation"] == "HeuristicIntentAgent" and intent["uses_llm"] is False
    assert query["implementation"] == "ExampleQueryAgent" and query["stand_in"]
    assert intent["json_mode"] is True and query["json_mode"] is False
    assert intent["max_answer_tokens"] == 200 and query["max_answer_tokens"] == 1500
    assert "secret-token-123" not in str(config) and intent["api_key"].startswith("set (")
    assert config["context"]["examples_in_bank"] == 16 and config["pipeline"]["schema_classes_loaded"] == 164


def test_preview_builds_the_real_prompts_without_calling_a_model(client):
    preview = client.post("/agents/preview", json={"text": "How many patients have cholera, by sex?"}).json()
    intent_messages, query_messages = preview["intent"]["messages"], preview["query"]["messages"]
    assert intent_messages[1]["content"] == "How many patients have cholera, by sex?"
    assert "Question: How many patients have cholera, by sex?" in query_messages[1]["content"]
    assert preview["query"]["estimated_prompt_tokens"] > preview["intent"]["estimated_prompt_tokens"] > 100
    assert len(preview["context"]["examples"]) == 3


def test_preview_rejects_empty_text(client):
    assert client.post("/agents/preview", json={"text": " "}).status_code == 400


def test_check_does_nothing_on_the_stub_backend(client):
    result = client.post("/agents/check").json()
    assert result == {"intent": {"status": "stub backend: no model server"},
                      "query": {"status": "stub backend: no model server"}}


def test_run_shows_what_each_agent_did_on_the_stub_backend(client):
    result = client.post("/agents/run", json={"text": "How many patients have cholera, and which ones are they?"}).json()
    assert result["backend"] == "stub" and result["is_compliant"] is False
    assert result["flags_raised"] == ["asks_for_identifiers"] and result["sparql_query"]
    assert {"intent", "generate", "guard", "total"} <= set(result["timings_ms"])


def test_run_rejects_empty_text(client):
    assert client.post("/agents/run", json={"text": " "}).status_code == 400


@pytest.fixture()
def reports_client(tmp_path):
    def save(name, content):
        (tmp_path / name).write_text(content if isinstance(content, str) else json.dumps(content))
    save("20261017-002316.json", {"summary": {"questions": 31, "verdict_accuracy": 0.903}, "rows": []})
    save("intent-20261017-090000.json", {"summary": {"questions": 31, "detection_rate": 1.0}, "rows": [{"id": "x"}]})
    save("pipeline-20261017-100000.json", "not json")
    save("notes.json", {"summary": {}})
    return TestClient(create_app(settings=Settings(data_dir=DATA_DIR, log_path=None, reports_dir=tmp_path)))


def test_reports_lists_eval_reports_newest_first(reports_client):
    listing = reports_client.get("/agents/reports").json()["reports"]
    assert [(r["name"], r["kind"]) for r in listing] == [("intent-20261017-090000.json", "intent"),
                                                          ("20261017-002316.json", "pipeline")]
    assert listing[0]["created"] == "2026-10-17 09:00" and listing[1]["summary"]["verdict_accuracy"] == 0.903


def test_report_detail_has_the_rows_and_refuses_other_files(reports_client):
    assert reports_client.get("/agents/reports/intent-20261017-090000.json").json()["rows"] == [{"id": "x"}]
    assert reports_client.get("/agents/reports/notes.json").status_code == 404
    assert reports_client.get("/agents/reports/pipeline-20261017-100000.json").status_code == 404
