import pytest

from conftest import DATA_DIR
from nl2sparql.agents.prompts import INTENT_PROMPTS, QUERY_PROMPTS, intent_prompt, query_prompt
from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline

SHARED = {
    "NL2SPARQL_BACKEND": "llm",
    "LLM_BASE_URL": "http://shared:18000/v1",
    "LLM_API_KEY": "shared-key",
    "LLM_QUERY_MODEL": "Qwen/Qwen3.5-9B",
    "LLM_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}',
}


@pytest.fixture()
def env(monkeypatch):
    for name in ("LLM_INTENT_MODEL", "LLM_INTENT_BASE_URL", "LLM_INTENT_API_KEY", "LLM_INTENT_EXTRA_BODY",
                 "LLM_INTENT_MAX_TOKENS", "INTENT_PROMPT_VERSION", "QUERY_PROMPT_VERSION"):
        monkeypatch.delenv(name, raising=False)
    for name, value in SHARED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_intent_agent_inherits_the_shared_settings(env):
    intent = Settings.from_env().agent("intent")
    assert intent.model == "Qwen/Qwen3.5-9B" and intent.base_url == "http://shared:18000/v1"
    assert intent.api_key == "shared-key" and intent.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert intent.max_tokens == 200 and intent.prompt_version == "v4"


def test_intent_agent_can_have_its_own_server_model_and_options(env):
    env.setenv("LLM_INTENT_MODEL", "swiss-ai/Apertus-8B-Instruct-2509")
    env.setenv("LLM_INTENT_BASE_URL", "http://intent:18001/v1")
    env.setenv("LLM_INTENT_EXTRA_BODY", "{}")
    env.setenv("LLM_INTENT_MAX_TOKENS", "120")
    settings = Settings.from_env()
    intent, query = settings.agent("intent"), settings.agent("query")
    assert (intent.model, intent.base_url, intent.extra_body, intent.max_tokens) == (
        "swiss-ai/Apertus-8B-Instruct-2509", "http://intent:18001/v1", {}, 120)
    assert (query.model, query.base_url) == ("Qwen/Qwen3.5-9B", "http://shared:18000/v1")


def test_factory_builds_one_client_per_agent(env):
    env.setenv("LLM_INTENT_BASE_URL", "http://intent:18001/v1")
    settings = Settings.from_env()
    pipeline = build_pipeline(Settings(**{**settings.__dict__, "data_dir": DATA_DIR, "log_path": None}))
    assert pipeline.intent_agent.client.base_url == "http://intent:18001/v1"
    assert pipeline.query_agent.client.base_url == "http://shared:18000/v1"
    assert pipeline.intent_agent.max_tokens == 200 and pipeline.query_agent.max_tokens == 1500
    assert type(pipeline.intent_agent.fallback).__name__ == "HeuristicIntentAgent"


def test_prompt_versions_are_selectable_and_checked():
    assert intent_prompt("v1") is INTENT_PROMPTS["v1"]
    assert {"v1", "v2", "v3"} <= set(INTENT_PROMPTS)
    assert query_prompt("v1") is QUERY_PROMPTS["v1"]
    with pytest.raises(ValueError, match="Available: v1"):
        query_prompt("v9")


def test_context_sizes_come_from_the_environment(env):
    env.setenv("CONTEXT_EXAMPLES", "2")
    env.setenv("CONTEXT_CARDS", "4")
    settings = Settings.from_env()
    pipeline = build_pipeline(Settings(**{**settings.__dict__, "data_dir": DATA_DIR, "log_path": None}))
    assert (pipeline.context_builder.k_examples, pipeline.context_builder.k_cards, pipeline.context_builder.k_codes) == (2, 4, 5)


def test_retry_budget_per_checker_comes_from_the_environment(env):
    env.setenv("MAX_RETRIES", "2")
    settings = Settings.from_env()
    pipeline = build_pipeline(Settings(**{**settings.__dict__, "data_dir": DATA_DIR, "log_path": None}))
    assert settings.max_retries == 2 and pipeline.max_retries == 2


def test_unknown_agent_name_is_rejected():
    with pytest.raises(ValueError, match="Unknown agent"):
        Settings().agent("judge")
