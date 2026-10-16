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
    assert intent["max_answer_tokens"] == 200 and query["max_answer_tokens"] == 900
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
