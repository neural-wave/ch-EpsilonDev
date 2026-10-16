"""Settings read from environment variables (see .env.example)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def _json_object(name: str) -> dict:
    value = os.environ.get(name, "").strip()
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must be a JSON object, e.g. '{{\"chat_template_kwargs\": {{\"enable_thinking\": false}}}}'") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return parsed


@dataclass(frozen=True)
class Settings:
    backend: str = "stub"  # "stub" or "llm"
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"
    llm_query_model: str = ""
    llm_intent_model: str = ""
    llm_timeout_seconds: float = 30.0
    llm_verify_tls: bool = True  # set LLM_VERIFY_TLS=false for self-signed certificates (Vast.ai)
    llm_extra_body: dict = field(default_factory=dict)  # LLM_EXTRA_BODY, JSON merged into every request
    virtuoso_endpoint: str = "http://localhost:8890/sparql"
    max_input_chars: int = 2000
    max_retries: int = 1
    # Hackathon: the jury asked for the literal query of forbidden requests so they can judge it.
    # In real use a forbidden request returns only the reason (set RETURN_NONCOMPLIANT_QUERY=false).
    return_noncompliant_query: bool = True
    data_dir: Path = field(default=REPO_ROOT / "data")
    assets_dir: Path = field(default=REPO_ROOT / "assets")
    log_path: Path | None = field(default=REPO_ROOT / "logs" / "requests.jsonl")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        query_model = env.get("LLM_QUERY_MODEL", "")
        return cls(
            backend=env.get("NL2SPARQL_BACKEND", "stub").strip().lower(),
            llm_base_url=env.get("LLM_BASE_URL", cls.llm_base_url),
            llm_api_key=env.get("LLM_API_KEY", cls.llm_api_key),
            llm_query_model=query_model,
            llm_intent_model=env.get("LLM_INTENT_MODEL", "") or query_model,
            llm_timeout_seconds=float(env.get("LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds)),
            llm_verify_tls=env.get("LLM_VERIFY_TLS", "true").strip().lower() not in ("0", "false", "no"),
            llm_extra_body=_json_object("LLM_EXTRA_BODY"),
            virtuoso_endpoint=env.get("VIRTUOSO_ENDPOINT", cls.virtuoso_endpoint),
            max_input_chars=int(env.get("MAX_INPUT_CHARS", cls.max_input_chars)),
            max_retries=int(env.get("MAX_RETRIES", cls.max_retries)),
            return_noncompliant_query=env.get("RETURN_NONCOMPLIANT_QUERY", "true").strip().lower()
            not in ("0", "false", "no"),
            data_dir=_path("NL2SPARQL_DATA_DIR", REPO_ROOT / "data"),
            assets_dir=_path("NL2SPARQL_ASSETS_DIR", REPO_ROOT / "assets"),
            log_path=_path("NL2SPARQL_LOG_PATH", REPO_ROOT / "logs" / "requests.jsonl"),
        )
