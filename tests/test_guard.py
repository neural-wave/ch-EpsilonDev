import pytest

from conftest import PREFIXES, load_examples
from nl2sparql.models import IntentFlags

EXAMPLES = load_examples()

# (name, expected_compliant, expected_rule or None, query)
CRAFTED = [
    ("ids listed", False, "R2", PREFIXES + "SELECT ?id WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id } }"),
    ("identifier literal", False, "R1", PREFIXES + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasIdentifier "ID-10125020000015" } }'),
    ("group by patient", False, "R5", PREFIXES + "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY ?g"),
    ("hidden count per patient", False, "R2", PREFIXES + "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY ?g"),
    ("group by timestamp", False, "R2", PREFIXES + "SELECT ?d (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasRecordDateTime ?d } } GROUP BY ?d"),
    ("group by year", True, None, PREFIXES + "SELECT ?y (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasRecordDateTime ?d } BIND(YEAR(?d) AS ?y) } GROUP BY ?y"),
    ("max identifier", False, "R5", PREFIXES + "SELECT (MAX(?id) AS ?x) WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id } }"),
    ("ask", False, "R4", PREFIXES + "ASK { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"),
    ("service", False, "R4", PREFIXES + "SELECT (COUNT(*) AS ?n) WHERE { SERVICE <https://other.example/sparql> { ?s ?p ?o } }"),
    ("nested patient graph hidden by count", True, None, PREFIXES + "SELECT (COUNT(*) AS ?n) WHERE { { SELECT DISTINCT ?g WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } }"),
    ("nested patient graphs concatenated", False, "R5", PREFIXES + "SELECT (GROUP_CONCAT(?g) AS ?patients) WHERE { { SELECT DISTINCT ?g WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } }"),
    ("nested record sampled", False, "R5", PREFIXES + "SELECT (SAMPLE(?record) AS ?one) WHERE { { SELECT DISTINCT ?record WHERE { GRAPH ?g { ?record a sphn:BilledDiagnosis } } } }"),
    ("delete", False, "R4", PREFIXES + "DELETE WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }"),
    ("whole graph", False, "R3", "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"),
    ("ontology only", True, None, PREFIXES + "SELECT ?code ?label WHERE { GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?code rdfs:subClassOf* icd-10-gm:E11 ; rdfs:label ?label } }"),
    ("str of graph", False, "R5", PREFIXES + "SELECT (STR(?g) AS ?pid) (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY ?g"),
    ("sex distribution", True, None, PREFIXES + "SELECT ?c (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s a sphn:AdministrativeSex ; sphn:hasCode ?code . ?code a ?c } } GROUP BY ?c"),
    ("select star", False, "R2", PREFIXES + "SELECT * WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"),
    ("rank distribution", True, None, PREFIXES + "SELECT ?rank (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasRank ?rank } } GROUP BY ?rank"),
    ("group by record", False, "R5", PREFIXES + "SELECT ?dx (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c } } GROUP BY ?dx"),
    ("record iri", False, "R1", PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { <https://biomedit.ch/rdf/sphn-resource/CHE-108_904_325-sphn-BilledDiagnosis-7ae563b3> sphn:hasCode ?c } }"),
    ("filter on identifier", False, "R1", PREFIXES + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id } FILTER(?id = "ID-1") }'),
    ("filter on graph name", False, "R1", PREFIXES + 'SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER(STR(?g) = "http://example.org/graph/gen_mock_data-1_1012502") }'),
    ("regex on graph name", False, "R1", PREFIXES + 'SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER(REGEX(STR(?g), "1012502$")) }'),
    ("regex on identifier", False, "R1", PREFIXES + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id } FILTER(REGEX(?id, "^ID-1012502")) }'),
    ("patient prefix filter", True, None, PREFIXES + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER(STRSTARTS(STR(?g), "http://example.org/graph/")) }'),
    ("patient default dataset", False, "R1", PREFIXES + "SELECT (COUNT(*) AS ?n) FROM <http://example.org/graph/patient-42> WHERE { ?s ?p ?o }"),
    ("patient named dataset", False, "R1", PREFIXES + "SELECT (COUNT(*) AS ?n) FROM NAMED <http://example.org/graph/patient-42> WHERE { GRAPH ?g { ?s a sphn:BilledDiagnosis } }"),
    ("single-patient HAVING group", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(DISTINCT ?g) = 1)"),
    ("single-patient HAVING below two", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(DISTINCT ?g) < 2)"),
    ("single-patient HAVING at most one", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(DISTINCT ?g) <= 1)"),
    ("single-patient HAVING constant first", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (2 > COUNT(DISTINCT ?g))"),
    ("single-patient HAVING IN", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(DISTINCT ?g) IN (1))"),
    ("single-row HAVING", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(*) = 1)"),
    ("single-record HAVING", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?dx) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(DISTINCT ?dx) = 1)"),
    ("single-patient HAVING in a conjunction", False, "R2", PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(*) > 0 && COUNT(DISTINCT ?g) = 1)"),
    ("one distinct category HAVING", True, None, PREFIXES + "SELECT ?rank (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasRank ?rank ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?rank HAVING (COUNT(DISTINCT ?diagnosis) = 1)"),
    ("minimum-two HAVING group", True, None, PREFIXES + "SELECT ?diagnosis (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?diagnosis } } GROUP BY ?diagnosis HAVING (COUNT(DISTINCT ?g) >= 2)"),
    ("group_concat of values", False, "R2", PREFIXES + "SELECT (GROUP_CONCAT(?v) AS ?vals) WHERE { GRAPH ?g { ?q sphn:hasValue ?v } }"),
    ("average value", True, None, PREFIXES + "SELECT (AVG(?v) AS ?mean) WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasQuantity/sphn:hasValue ?v } }"),
    ("first date", True, None, PREFIXES + "SELECT (MIN(?d) AS ?first) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasRecordDateTime ?d } }"),
    ("count per class", False, "R3", "SELECT ?class (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s a ?class } } GROUP BY ?class"),
]


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_official_examples_match_their_label(guard, example):
    violations = guard.check_query(example["sparql"])
    assert (not violations) == example["compliant"], violations


@pytest.mark.parametrize("name,compliant,rule,query", CRAFTED, ids=[c[0] for c in CRAFTED])
def test_crafted_cases(guard, name, compliant, rule, query):
    violations = guard.check_query(query)
    assert (not violations) == compliant, violations
    if rule:
        assert rule in {v.rule for v in violations}


def _example(number: int) -> str:
    return next(e["sparql"] for e in EXAMPLES if e["id"] == f"official-{number:02d}")


def test_clean_query_and_clean_request_is_compliant(guard):
    decision = guard.decide(_example(3), IntentFlags())
    assert decision.compliant and decision.reason is None and decision.retry_hint is None


def test_flagged_request_with_broken_query_is_not_retried(guard):
    flags = IntentFlags(asks_for_identifiers=True, reason="asks which patients have cholera",
                        suggested_rephrase="How many patients have cholera?")
    decision = guard.decide(_example(15), flags)
    assert not decision.compliant and decision.retry_hint is None
    assert decision.reason.startswith("The request asks which patients have cholera.")
    assert "R5" in {v.rule for v in decision.violations}
    assert decision.reason.endswith('Try instead: "How many patients have cholera?"')


def test_flagged_request_with_clean_query_stays_non_compliant(guard):
    decision = guard.decide(_example(3), IntentFlags(asks_for_identifiers=True))
    assert not decision.compliant and decision.retry_hint is None and not decision.violations
    assert "identifiers, which must never be exposed" in decision.reason
    assert decision.suggested_rephrase == "Ask how many patients, not who they are."


def test_allowed_request_with_broken_query_gets_a_retry_hint(guard):
    decision = guard.decide(_example(12), IntentFlags())
    assert not decision.compliant
    assert decision.retry_hint and "aggregated" in decision.retry_hint


GROUPING_CASES = [
    ("group by year expression", True, PREFIXES + "SELECT (YEAR(?dt) AS ?year) (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?e a sphn:LabTestEvent ; sphn:hasDateTime ?dt } } GROUP BY (YEAR(?dt))"),
    ("sample of exact date", False, PREFIXES + "SELECT (SAMPLE(?dt) AS ?when) (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?e a sphn:LabTestEvent ; sphn:hasDateTime ?dt } }"),
    ("class also used as subject", True, PREFIXES + "SELECT ?m (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?p a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . OPTIONAL { ?concept rdfs:label ?label } } BIND(REPLACE(STR(?concept), \"^.*/\", \"\") AS ?m) } GROUP BY ?m"),
]


@pytest.mark.parametrize("name,compliant,query", GROUPING_CASES, ids=[c[0] for c in GROUPING_CASES])
def test_grouping_cases_found_by_the_llm_eval(guard, name, compliant, query):
    violations = guard.check_query(query)
    assert (not violations) == compliant, violations
