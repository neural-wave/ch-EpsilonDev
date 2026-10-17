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


def _json_object(name: str) -> dict | None:
    """A JSON object from the environment, or None when the variable is unset or blank."""
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must be a JSON object, e.g. '{{\"chat_template_kwargs\": {{\"enable_thinking\": false}}}}'") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return parsed


def _minimum_group_size(value: str | None) -> int | None:
    if value is None or value.strip().lower() in ("", "disabled"):
        return None
    try:
        threshold = int(value)
    except ValueError:
        raise ValueError("DEMO_MIN_GROUP_SIZE must be a positive integer or disabled.") from None
    if threshold < 1:
        raise ValueError("DEMO_MIN_GROUP_SIZE must be a positive integer or disabled.")
    return threshold


def _optional_text(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() not in ("0", "false", "no")


@dataclass(frozen=True)
class AgentSettings:
    """Everything one LLM agent uses, after per-agent overrides fall back to the shared LLM_* values."""

    name: str  # "intent" or "query"
    model: str
    base_url: str
    api_key: str
    extra_body: dict
    max_tokens: int
    prompt_version: str
    timeout_seconds: float
    verify_tls: bool


@dataclass(frozen=True)
class Settings:
    demo_public_mode: bool = False
    demo_min_group_size: int | None = None
    demo_dataset_name: str | None = None
    demo_dataset_version: str | None = None
    demo_dataset_description: str | None = None
    demo_share_ttl_seconds: int = 7 * 24 * 3600
    demo_share_db_path: Path = field(default=REPO_ROOT / "logs" / "shared_results.sqlite3")
    backend: str = "stub"  # "stub" or "llm"
    # Shared LLM settings: both agents use them unless overridden below.
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"
    llm_query_model: str = ""
    llm_timeout_seconds: float = 30.0
    request_timeout_seconds: float = 75.0  # whole request, retries included; expiry answers HTTP 503
    llm_verify_tls: bool = True  # set LLM_VERIFY_TLS=false for self-signed certificates (Vast.ai)
    llm_extra_body: dict = field(default_factory=dict)  # LLM_EXTRA_BODY, JSON merged into every request
    # Intent agent overrides (empty or None = use the shared value).
    llm_intent_model: str = ""
    llm_intent_base_url: str = ""
    llm_intent_api_key: str = ""
    llm_intent_extra_body: dict | None = None
    # Per-agent generation limits and prompt versions.
    llm_intent_max_tokens: int = 200
    llm_query_max_tokens: int = 1500  # 900 cut off 6 of 198 answers on 2026-10-17; only used when needed
    # Code pass after the query agent: typed code patterns first, join order locked (see sparql/order.py).
    query_reorder: bool = True  # the restricted pass: measured safe on dev, one win on 100k patients (q-021 0.44 -> 0.06 s)
    intent_prompt_version: str = "v4"  # won the intent eval on 2026-10-17 (docs/LLM_AGENTS.md section 6)
    query_prompt_version: str = "v4"  # halves the blocked allowed requests of v2 (2026-10-17)
    # Context the query agent receives: similar examples, schema cards, code candidates.
    context_examples: int = 3
    context_cards: int = 8  # the picker now follows one link from a named class; two more slots keep that from pushing out named classes
    context_codes: int = 5
    # Pipeline.
    virtuoso_endpoint: str = "http://localhost:8890/sparql"
    max_input_chars: int = 2000
    max_retries: int = 1  # per checker: the judge may ask the model again this often, and so may the guard
    # The retry feedback can include the rejected query. Measured on 2026-10-17 and switched off: the
    # model copies the broken query instead of fixing it (judge retries ending valid 8/18 -> 3/19).
    retry_with_previous_query: bool = False
    # Hackathon: the jury asked for the literal query of forbidden requests so they can judge it.
    # In real use a forbidden request returns only the reason (set RETURN_NONCOMPLIANT_QUERY=false).
    return_noncompliant_query: bool = True
    data_dir: Path = field(default=REPO_ROOT / "data")
    assets_dir: Path = field(default=REPO_ROOT / "assets")
    log_path: Path | None = field(default=REPO_ROOT / "logs" / "requests.jsonl")
    reports_dir: Path = field(default=REPO_ROOT / "eval" / "reports")  # written by eval/, read by /agents

    def agent(self, name: str) -> AgentSettings:
        """Resolved settings of one agent: "intent" or "query"."""
        if name == "intent":
            return AgentSettings(
                name="intent",
                model=self.llm_intent_model or self.llm_query_model,
                base_url=self.llm_intent_base_url or self.llm_base_url,
                api_key=self.llm_intent_api_key or self.llm_api_key,
                extra_body=self.llm_extra_body if self.llm_intent_extra_body is None else self.llm_intent_extra_body,
                max_tokens=self.llm_intent_max_tokens,
                prompt_version=self.intent_prompt_version,
                timeout_seconds=self.llm_timeout_seconds,
                verify_tls=self.llm_verify_tls,
            )
        if name == "query":
            return AgentSettings(
                name="query",
                model=self.llm_query_model,
                base_url=self.llm_base_url,
                api_key=self.llm_api_key,
                extra_body=self.llm_extra_body,
                max_tokens=self.llm_query_max_tokens,
                prompt_version=self.query_prompt_version,
                timeout_seconds=self.llm_timeout_seconds,
                verify_tls=self.llm_verify_tls,
            )
        raise ValueError(f"Unknown agent {name!r}: use 'intent' or 'query'.")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            demo_public_mode=_flag("DEMO_PUBLIC_MODE", False),
            demo_dataset_name=_optional_text("DEMO_DATASET_NAME"),
            demo_dataset_version=_optional_text("DEMO_DATASET_VERSION"),
            demo_dataset_description=_optional_text("DEMO_DATASET_DESCRIPTION"),
            demo_share_ttl_seconds=int(env.get("DEMO_SHARE_TTL_SECONDS", 7 * 24 * 3600)),
            demo_share_db_path=_path("DEMO_SHARE_DB_PATH", REPO_ROOT / "logs" / "shared_results.sqlite3"),
            demo_min_group_size=_minimum_group_size(env.get("DEMO_MIN_GROUP_SIZE")),
            backend=env.get("NL2SPARQL_BACKEND", "stub").strip().lower(),
            llm_base_url=env.get("LLM_BASE_URL", cls.llm_base_url),
            llm_api_key=env.get("LLM_API_KEY", cls.llm_api_key),
            llm_query_model=env.get("LLM_QUERY_MODEL", ""),
            llm_timeout_seconds=float(env.get("LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds)),
            request_timeout_seconds=float(env.get("REQUEST_TIMEOUT_SECONDS", cls.request_timeout_seconds)),
            llm_verify_tls=_flag("LLM_VERIFY_TLS", True),
            llm_extra_body=_json_object("LLM_EXTRA_BODY") or {},
            llm_intent_model=env.get("LLM_INTENT_MODEL", ""),
            llm_intent_base_url=env.get("LLM_INTENT_BASE_URL", ""),
            llm_intent_api_key=env.get("LLM_INTENT_API_KEY", ""),
            llm_intent_extra_body=_json_object("LLM_INTENT_EXTRA_BODY"),
            llm_intent_max_tokens=int(env.get("LLM_INTENT_MAX_TOKENS", cls.llm_intent_max_tokens)),
            llm_query_max_tokens=int(env.get("LLM_QUERY_MAX_TOKENS", cls.llm_query_max_tokens)),
            query_reorder=_flag("QUERY_REORDER", True),
            intent_prompt_version=env.get("INTENT_PROMPT_VERSION", cls.intent_prompt_version).strip(),
            query_prompt_version=env.get("QUERY_PROMPT_VERSION", cls.query_prompt_version).strip(),
            context_examples=int(env.get("CONTEXT_EXAMPLES", cls.context_examples)),
            context_cards=int(env.get("CONTEXT_CARDS", cls.context_cards)),
            context_codes=int(env.get("CONTEXT_CODES", cls.context_codes)),
            virtuoso_endpoint=env.get("VIRTUOSO_ENDPOINT", cls.virtuoso_endpoint),
            max_input_chars=int(env.get("MAX_INPUT_CHARS", cls.max_input_chars)),
            max_retries=int(env.get("MAX_RETRIES", cls.max_retries)),
            retry_with_previous_query=_flag("RETRY_WITH_PREVIOUS_QUERY", False),
            return_noncompliant_query=_flag("RETURN_NONCOMPLIANT_QUERY", True),
            data_dir=_path("NL2SPARQL_DATA_DIR", REPO_ROOT / "data"),
            assets_dir=_path("NL2SPARQL_ASSETS_DIR", REPO_ROOT / "assets"),
            log_path=_path("NL2SPARQL_LOG_PATH", REPO_ROOT / "logs" / "requests.jsonl"),
            reports_dir=_path("NL2SPARQL_REPORTS_DIR", REPO_ROOT / "eval" / "reports"),
        )
