import asyncio
import json

import httpx
import pytest

from conftest import DATA_DIR
from nl2sparql.agents import ExampleQueryAgent, HeuristicIntentAgent, LLMIntentAgent, LLMQueryAgent
from nl2sparql.context import SchemaCards
from nl2sparql.judge import Judge
from nl2sparql.llm import LLMClient
from nl2sparql.models import Context, Example


def run(coroutine):
    return asyncio.run(coroutine)


def test_heuristic_intent_flags_the_official_forbidden_requests():
    agent = HeuristicIntentAgent()
    flags, tokens = run(agent.classify("How many patients have cholera, and which ones are they?"))
    assert flags.asks_for_identifiers and tokens == 0
    assert flags.suggested_rephrase == "How many patients have cholera?"
    assert run(agent.classify("How many lab tests has patient 0042 had?"))[0].single_patient
    assert run(agent.classify("How much data do we have in total?"))[0].whole_graph
    assert not run(agent.classify("How many patients have a billed diagnosis of cholera?"))[0].flagged


def test_example_agent_returns_the_top_example():
    context = Context(examples=[Example("x", "q", "SELECT * WHERE { ?s ?p ?o }", True)])
    assert run(ExampleQueryAgent().generate("q", context)) == ("SELECT * WHERE { ?s ?p ?o }", 0)


def fake_server(reply_text: str, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": reply_text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })
    return httpx.MockTransport(handler)


def test_llm_intent_agent_parses_json_and_counts_tokens():
    seen = []
    reply = '{"asks_for_identifiers": true, "reason": "asks which patients", "suggested_rephrase": "How many?"}'
    agent = LLMIntentAgent(LLMClient("http://gpu/v1", transport=fake_server(reply, seen)), "small-model")
    flags, tokens = run(agent.classify("Which patients have cholera?"))
    assert flags.asks_for_identifiers and not flags.single_patient and tokens == 120
    assert seen[0]["model"] == "small-model" and seen[0]["response_format"] == {"type": "json_object"}


def test_llm_client_merges_extra_body():
    seen = []
    client = LLMClient("http://gpu/v1", transport=fake_server("{}", seen),
                       extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    run(LLMIntentAgent(client, "m").classify("hi"))
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False} and seen[0]["model"] == "m"


def test_llm_query_agent_extracts_sparql_from_a_fenced_answer():
    seen = []
    agent = LLMQueryAgent(LLMClient("http://gpu/v1", transport=fake_server("Here:\n```sparql\nSELECT ?x WHERE { ?x ?p ?o }\n```", seen)), "coder")
    sparql, tokens = run(agent.generate("q", Context(), feedback="add an aggregate"))
    assert sparql == "SELECT ?x WHERE { ?x ?p ?o }\n" and tokens == 120
    assert "previous query was rejected: add an aggregate" in seen[0]["messages"][1]["content"]


def test_judge_fixes_prefixes_and_flags_terms_missing_from_the_data():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    ok = judge.check("SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasRank ?r } }")
    assert ok.ok and ok.sparql.startswith("PREFIX sphn:")
    wrong = judge.check("SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d sphn:hasRankCode ?r } }")
    assert wrong.errors == ["sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?"]
    invalid = judge.check("SELECT ?x WHERE {")
    assert invalid.errors and invalid.errors[0].startswith("Syntax error:")
    corrected = judge.check("SELECT ?x WHERE { VALUES ?x { 1 } }")
    assert corrected.ok


def test_judge_suggests_unknown_term_inside_nested_query():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        "{ SELECT ?dx WHERE { ?dx sphn:hasRankCode ?rank } } "
        "?dx a sphn:BilledDiagnosis } }"
    )
    result = judge.check(query)
    assert result.errors == [
        "sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?"
    ]
    near_miss = judge.check(query.replace("sphn:hasRankCode", "sphn:hasRank"))
    assert near_miss.ok, near_miss.errors


NESTED_TERM_CASES = [
    (
        "optional",
        "OPTIONAL { ?dx sphn:hasRankCode ?rank }",
        "sphn:hasRankCode",
        "sphn:hasRank",
        "sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?",
    ),
    (
        "union",
        "{ ?dx sphn:hasRankCode ?rank } UNION { ?dx sphn:hasRank ?rank }",
        "sphn:hasRankCode",
        "sphn:hasRank",
        "sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?",
    ),
    (
        "values",
        "VALUES ?class { sphn:BilledDiagnoses }",
        "sphn:BilledDiagnoses",
        "sphn:BilledDiagnosis",
        "sphn:BilledDiagnoses does not occur in the data. Did you mean sphn:BilledDiagnosis?",
    ),
    (
        "bind",
        "BIND(sphn:hasRankCode AS ?predicate)",
        "sphn:hasRankCode",
        "sphn:hasRank",
        "sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?",
    ),
]


@pytest.mark.parametrize(
    "scope,pattern,unknown,known,expected_error",
    NESTED_TERM_CASES,
    ids=[case[0] for case in NESTED_TERM_CASES],
)
def test_judge_checks_unknown_terms_in_nested_graph_patterns(
    scope, pattern, unknown, known, expected_error
):
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { { "
        "SELECT ?dx WHERE { ?dx a sphn:BilledDiagnosis . "
        f"{pattern} }} }} }} }}"
    )
    result = judge.check(query)
    assert result.errors == [expected_error]
    near_miss = judge.check(query.replace(unknown, known))
    assert near_miss.ok, near_miss.errors


def test_judge_accepts_define_and_inference_extensions_together():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        'DEFINE sql:select-option "order"\n'
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        "?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . "
        '?code a icd-10-gm:A00 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") '
        "} }"
    )
    assert judge.check(query).ok


def test_judge_requires_inference_on_icd_code_type_patterns():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        "?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . "
        "?code a icd-10-gm:A00 } }"
    )
    result = judge.check(query)
    message = (
        "Every ICD-10-GM or CHOP code type pattern needs "
        'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global").'
    )
    assert result.errors == [message]
    fixed = query.replace(
        "?code a icd-10-gm:A00 }",
        '?code a icd-10-gm:A00 OPTION '
        '(inference "https://biomedit.ch/rdf/sphn-resource/global") }',
    )
    assert judge.check(fixed).ok


def test_judge_accepts_code_pattern_with_inference_option():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        "?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . "
        "?code a icd-10-gm:A00 OPTION (inference "
        "\"https://biomedit.ch/rdf/sphn-resource/global\") } }"
    )
    assert judge.check(query).ok


def test_each_typed_code_pattern_needs_its_own_inference_option():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    root = "https://biomedit.ch/rdf/sphn-resource/global"
    query = (
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "PREFIX chop: <https://biomedit.ch/rdf/sphn-resource/chop/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        f"?a a icd-10-gm:A00 OPTION (inference \"{root}\") . "
        "?b a chop:Q01 } }"
    )
    result = judge.check(query)
    assert any("Every ICD-10-GM or CHOP code type pattern needs" in error for error in result.errors)


def test_each_typed_code_pattern_accepts_its_own_inference_option():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    root = "https://biomedit.ch/rdf/sphn-resource/global"
    query = (
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "PREFIX chop: <https://biomedit.ch/rdf/sphn-resource/chop/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        f"?a a icd-10-gm:A00 OPTION (inference \"{root}\") . "
        f"?b a chop:Q01 OPTION (inference \"{root}\") "
        "} }"
    )
    assert judge.check(query).ok


def test_repeated_code_pattern_in_nested_scope_needs_repeated_option():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    root = "https://biomedit.ch/rdf/sphn-resource/global"
    query = (
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { { "
        f"?code a icd-10-gm:A00 OPTION (inference \"{root}\") "
        "} UNION { ?code a icd-10-gm:A00 } } }"
    )
    result = judge.check(query)
    assert result.errors == [
        "Every ICD-10-GM or CHOP code type pattern needs "
        'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global").'
    ]
    corrected = query.replace(
        "?code a icd-10-gm:A00 }",
        f'?code a icd-10-gm:A00 OPTION (inference "{root}") }}',
    )
    assert judge.check(corrected).ok


def test_inference_option_on_non_code_pattern_does_not_satisfy_code_pattern():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    root = "https://biomedit.ch/rdf/sphn-resource/global"
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        f"?dx a sphn:BilledDiagnosis OPTION (inference \"{root}\") . "
        "?code a icd-10-gm:A00 } }"
    )
    assert any("Every ICD-10-GM or CHOP code type pattern needs" in error
               for error in judge.check(query).errors)


def test_judge_warns_on_non_distinct_patient_graph_count():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"
    )
    result = judge.check(query)
    assert result.errors == [
        "Count patients with COUNT(DISTINCT ?g) to avoid counting duplicate graph matches."
    ]


def test_judge_accepts_distinct_patient_count_and_distinct_graph_subquery():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    direct = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"
    )
    subquery = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(?g) AS ?n) WHERE { { SELECT DISTINCT ?g WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } }"
    )
    assert judge.check(direct).ok
    assert judge.check(subquery).ok


def test_unrelated_distinct_subquery_does_not_silence_graph_count_warning():
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(?g) AS ?n) WHERE { { SELECT DISTINCT ?g WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } "
        "GRAPH ?g { ?event a sphn:LabTestEvent } }"
    )
    assert any("COUNT(DISTINCT ?g)" in error for error in judge.check(query).errors)


def test_judge_tolerates_missing_schema_snapshot():
    judge = Judge(SchemaCards({}, []))
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:UnknownClass } }"
    )
    assert judge.check(query).ok


def test_guard_and_judge_do_not_access_virtuoso_with_missing_metadata(monkeypatch, guard):
    import nl2sparql.virtuoso

    def fail_if_called(*args, **kwargs):
        raise AssertionError("request-time Guard/Judge must not contact Virtuoso")

    monkeypatch.setattr(nl2sparql.virtuoso, "select", fail_if_called)
    query = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
    assert Judge(SchemaCards({}, [])).check(query).ok
    assert "R3" in {violation.rule for violation in guard.check_query(query)}
