import json
from pathlib import Path

import pytest
from rdflib import URIRef, Variable

from conftest import DATA_DIR, PREFIXES, load_examples
from nl2sparql.context import SchemaCards
from nl2sparql.guard import ComplianceGuard, PropertyKinds, RuleSettings
from nl2sparql.guard.decision import AGGREGATION_ADVICE, GROUPING_ADVICE, REQUEST_LEVEL_RULES
from nl2sparql.judge import Judge
from nl2sparql.models import IntentFlags
from nl2sparql.guard.provenance import Sensitivity, analyse
from nl2sparql.sparql.parse import parse_sparql

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
    ("identifier literal inside NOT EXISTS", False, "R1", PREFIXES + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER NOT EXISTS { GRAPH ?g { ?s sphn:hasIdentifier "ID-10125020000015" } } }'),
    ("record IRI inside EXISTS", False, "R1", PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER EXISTS { GRAPH ?g { <https://biomedit.ch/rdf/sphn-resource/CHE-108_904_325-sphn-BilledDiagnosis-7ae563b3> sphn:hasCode ?c } } }"),
    ("patients without a record", True, None, PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER NOT EXISTS { GRAPH ?g { ?d a sphn:Death } } }"),
    ("patient pattern hidden in EXISTS", False, "R2", PREFIXES + "SELECT ?code WHERE { GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?code rdfs:subClassOf icd-10-gm:A00 } FILTER EXISTS { GRAPH ?g { ?dx sphn:hasCode ?c . ?c a ?code } } }"),
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


# Additional trigger/near-miss pairs for Guard interaction audit
# These assert: trigger rejects, near-miss accepts, with diagnostic code, reason, and retry behavior
RULE_TRIGGER_NEAR_MISSES_EXTRA = [
    # Nested aggregate projection
    (
        "R5",
        PREFIXES + "SELECT ?g WHERE { { SELECT ?g WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        "nested aggregate projection",
    ),
    # Identifier filter / BIND interaction
    (
        "R1",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id } BIND(MD5(STR(?id)) AS ?fingerprint) FILTER(?fingerprint = \"synthetic-hash\") } } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasRank ?rank } }",
        "identifier filter / BIND",
    ),
    # Inverse property path trigger
    (
        "R1",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { \"synthetic-key\" ^sphn:hasIdentifier ?subject } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { \"synthetic-value\" ^sphn:hasValue ?quantity } }",
        "inverse identifier path",
    ),
    # Alternative property path trigger
    (
        "R5",
        PREFIXES + "SELECT (MAX(?id) AS ?x) WHERE { GRAPH ?g { ?record (sphn:hasIdentifier|sphn:hasSharedIdentifier) ?id } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasRank ?rank } GROUP BY ?rank }",
        "alternative identifier path",
    ),
    # Nested graph path filter and aggregate interaction
    (
        "R1",
        PREFIXES + "SELECT (MAX(?identifier) AS ?selected) WHERE { { SELECT ?g ?identifier WHERE { GRAPH ?g { ?record sphn:hasSubjectPseudoIdentifier/sphn:hasIdentifier ?identifier } BIND(SHA256(STR(?identifier)) AS ?fingerprint) FILTER(?fingerprint = \"synthetic-hash\") } } }",
        PREFIXES + "SELECT ?rank (COUNT(DISTINCT ?g) AS ?patients) WHERE { { SELECT ?g ?rank WHERE { GRAPH ?g { ?record sphn:hasRank ?rank } FILTER(?rank = sphn:Principal) } } GROUP BY ?rank }",
        "nested graph path filter and aggregate",
    ),
]


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


def test_r1_uses_the_supplied_property_classifier_for_literal_lookups():
    predicate = URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasOpaqueKey")

    class SyntheticIdentifierKinds(PropertyKinds):
        def object_sensitivity(self, candidate):
            if candidate == predicate:
                return Sensitivity.IDENTIFIER
            return super().object_sensitivity(candidate)

    custom_guard = ComplianceGuard(SyntheticIdentifierKinds())
    query = PREFIXES + (
        'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { '
        '?record sphn:hasOpaqueKey "synthetic-key" } }'
    )

    decision = custom_guard.decide(query, IntentFlags())

    assert not decision.compliant
    assert any(violation.rule == "R1" for violation in decision.violations)
    assert "looks up the identifier" in decision.reason.lower()


IDENTITY_TRANSFORMS = [
    ("concat", 'CONCAT(STR(?id), "-suffix")'),
    ("substring", "SUBSTR(STR(?id), 1, 4)"),
    ("record string", "STR(?record)"),
    ("md5", "MD5(STR(?id))"),
    ("sha256", "SHA256(STR(?id))"),
]


@pytest.mark.parametrize("name,expression", IDENTITY_TRANSFORMS, ids=[x[0] for x in IDENTITY_TRANSFORMS])
def test_identity_transforms_cannot_launder_identifiers(guard, name, expression):
    if name == "record string":
        pattern = "?record a sphn:BilledDiagnosis"
    else:
        pattern = "?s sphn:hasIdentifier ?id"
    leaked = PREFIXES + (
        f"SELECT ({expression} AS ?out) WHERE {{ GRAPH ?g {{ {pattern} }} }}"
    )
    violations = guard.check_query(leaked)
    assert "R5" in {violation.rule for violation in violations}
    assert any("identifier" in v.message.lower() or "record iri" in v.message.lower()
               for v in violations if v.rule == "R5")

    aggregated = PREFIXES + (
        f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ {pattern} }} "
        f"BIND({expression} AS ?internal) }}"
    )
    assert guard.check_query(aggregated) == []


def test_from_named_patient_graph_is_rejected_and_ontology_graph_is_allowed(guard):
    patient_graph = (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) "
        "FROM NAMED <http://example.org/graph/synthetic-person> "
        "WHERE { GRAPH ?g { ?s ?p ?o } }"
    )
    violations = guard.check_query(patient_graph)
    assert "R1" in {violation.rule for violation in violations}
    assert any("single patient graph" in v.message for v in violations if v.rule == "R1")

    ontology_graph = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        "SELECT ?label "
        "FROM NAMED <https://biomedit.ch/rdf/sphn-resource/global> "
        "WHERE { GRAPH <https://biomedit.ch/rdf/sphn-resource/global> "
        "{ ?concept rdfs:label ?label } }"
    )
    assert guard.check_query(ontology_graph) == []


def test_nested_patient_projection_is_safe_only_when_outer_result_aggregates(guard):
    inner_projection = (
        "SELECT ?g WHERE { { SELECT ?g WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } }"
    )
    violations = guard.check_query(PREFIXES + inner_projection)
    assert "R5" in {violation.rule for violation in violations}
    assert any("identifier" in v.message.lower() or "patient" in v.message.lower()
               for v in violations if v.rule == "R5")

    outer_aggregate = (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } } } }"
    )
    assert guard.check_query(PREFIXES + outer_aggregate) == []


def test_raw_record_iri_projection_triggers_both_row_and_identifier_rules(guard):
    query = PREFIXES + (
        "SELECT ?record WHERE { GRAPH ?g { ?record a sphn:BilledDiagnosis } }"
    )
    violations = guard.check_query(query)
    assert {violation.rule for violation in violations} >= {"R2", "R5"}
    assert any("patient or record identifiers" in v.message for v in violations if v.rule == "R5")


@pytest.mark.parametrize("branching", ["OPTIONAL", "UNION"], ids=["optional", "union"])
def test_identifier_sensitivity_flows_through_optional_and_union(guard, branching):
    if branching == "OPTIONAL":
        body = (
            "GRAPH ?g { ?dx a sphn:BilledDiagnosis } OPTIONAL "
            "{ GRAPH ?g { ?s sphn:hasIdentifier ?id } }"
        )
    else:
        body = (
            "{ GRAPH ?g { ?s sphn:hasIdentifier ?id } } UNION "
            "{ GRAPH ?g { ?s sphn:hasSharedIdentifier ?id } }"
        )
    leaked = PREFIXES + f"SELECT (MAX(?id) AS ?x) WHERE {{ {body} }}"
    violations = guard.check_query(leaked)
    assert "R5" in {violation.rule for violation in violations}
    assert any("identifier" in v.message.lower() for v in violations if v.rule == "R5")

    safe = PREFIXES + f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ {body} }}"
    assert guard.check_query(safe) == []


def test_nested_filter_and_bind_keep_identifier_sensitivity(guard):
    leaking = PREFIXES + (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g WHERE { "
        "GRAPH ?g { ?s sphn:hasIdentifier ?id } "
        "BIND(MD5(STR(?id)) AS ?fingerprint) "
        'FILTER(?fingerprint = "synthetic-hash") } } }'
    )
    decision = guard.decide(leaking, IntentFlags())
    assert not decision.compliant
    assert any(violation.rule == "R1" for violation in decision.violations)
    assert "compares the identifier" in decision.reason.lower()
    assert decision.retry_hint is None  # R1: the value came from the request, never retried

    category_near_miss = PREFIXES + (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g WHERE { "
        "GRAPH ?g { ?s sphn:hasRank ?rank } "
        "BIND(STR(?rank) AS ?category) "
        'FILTER(?category = "Principal") } } }'
    )
    accepted = guard.decide(category_near_miss, IntentFlags())
    assert accepted.compliant, accepted.violations
    assert accepted.reason is None and accepted.retry_hint is None


def test_nested_aggregate_is_safe_only_when_patient_level_value_is_not_projected(guard):
    nested = (
        "{ SELECT ?g (COUNT(?dx) AS ?perPatient) WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY ?g }"
    )
    safe = PREFIXES + (
        f"SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE {{ {nested} }}"
    )
    assert guard.check_query(safe) == []

    exposed = PREFIXES + f"SELECT ?g ?perPatient WHERE {{ {nested} }}"
    violations = guard.check_query(exposed)
    assert {violation.rule for violation in violations} >= {"R2", "R5"}
    assert any("individual rows" in violation.message for violation in violations if violation.rule == "R2")
    assert any("patient or record identifiers" in violation.message for violation in violations if violation.rule == "R5")


def test_inverse_identifier_path_literal_lookup_triggers_r1(guard):
    leaking = PREFIXES + (
        'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { '
        '"synthetic-key" ^sphn:hasIdentifier ?subject } }'
    )
    decision = guard.decide(leaking, IntentFlags())
    assert not decision.compliant
    assert "R1" in {violation.rule for violation in decision.violations}
    assert "looks up the identifier" in decision.reason.lower()
    assert decision.retry_hint is None

    value_near_miss = PREFIXES + (
        'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { '
        '"synthetic-value" ^sphn:hasValue ?quantity } }'
    )
    accepted = guard.decide(value_near_miss, IntentFlags())
    assert accepted.compliant and accepted.reason is None and accepted.retry_hint is None


def test_alternative_identifier_path_preserves_sensitive_object_kind(guard):
    leaking = PREFIXES + (
        "SELECT (MAX(?id) AS ?x) WHERE { GRAPH ?g { "
        "?record (sphn:hasIdentifier|sphn:hasSharedIdentifier) ?id } }"
    )
    violations = guard.check_query(leaking)
    assert "R5" in {violation.rule for violation in violations}

    category_near_miss = PREFIXES + (
        "SELECT ?category (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        "?record (sphn:hasRank|sphn:hasOutcome) ?category } } GROUP BY ?category"
    )
    assert guard.check_query(category_near_miss) == []


def test_nested_graph_path_filter_and_aggregate_interaction(guard):
    leaking = PREFIXES + (
        'SELECT (MAX(?identifier) AS ?selected) WHERE { { SELECT ?g ?identifier WHERE { '
        'GRAPH ?g { ?record sphn:hasSubjectPseudoIdentifier/sphn:hasIdentifier ?identifier } '
        'BIND(SHA256(STR(?identifier)) AS ?fingerprint) '
        'FILTER(?fingerprint = "synthetic-hash") } } }'
    )
    decision = guard.decide(leaking, IntentFlags())
    assert not decision.compliant
    assert {violation.rule for violation in decision.violations} >= {"R1", "R5"}
    assert "identifier" in decision.reason.lower()
    assert decision.retry_hint is None

    category_near_miss = PREFIXES + (
        'SELECT ?rank (COUNT(DISTINCT ?g) AS ?patients) WHERE { { SELECT ?g ?rank WHERE { '
        'GRAPH ?g { ?record sphn:hasRank ?rank } FILTER(?rank = sphn:Principal) } } } '
        'GROUP BY ?rank'
    )
    accepted = guard.decide(category_near_miss, IntentFlags())
    assert accepted.compliant, accepted.violations
    assert accepted.reason is None and accepted.retry_hint is None


RULE_TRIGGER_NEAR_MISSES = [
    (
        "R1",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { "
        "VALUES ?g { <http://example.org/graph/synthetic-person> } "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { "
        "VALUES ?concept { <http://example.org/concept/synthetic-category> } "
        "GRAPH ?g { ?record sphn:hasCode ?concept } }",
        "single patient graph",
    ),
    (
        "R2",
        PREFIXES + "SELECT ?dx WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        "individual rows",
    ),
    (
        "R3",
        "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        "whole graph",
    ),
    (
        "R4",
        PREFIXES + "ASK { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { "
        "GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
        "ASK query",
    ),
    (
        "R5",
        PREFIXES + "SELECT (MAX(?id) AS ?x) WHERE { GRAPH ?g { "
        "?s sphn:hasSubjectPseudoIdentifier ?id } }",
        PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { "
        "GRAPH ?g { ?s sphn:hasSubjectPseudoIdentifier ?id } }",
        "identifiers or record IRIs",
    ),
]


@pytest.mark.parametrize("rule,trigger,near_miss,reason", RULE_TRIGGER_NEAR_MISSES)
def test_each_guard_rule_has_reasoned_trigger_and_valid_near_miss(
    guard, rule, trigger, near_miss, reason
):
    rejected = guard.decide(trigger, IntentFlags())
    assert not rejected.compliant
    matching = [violation for violation in rejected.violations if violation.rule == rule]
    assert matching and reason.lower() in " ".join(v.message for v in matching).lower()
    assert rejected.reason and reason.lower() in rejected.reason.lower()
    if rule in REQUEST_LEVEL_RULES:
        assert rejected.retry_hint is None
    else:
        assert rejected.retry_hint and reason.lower() in rejected.retry_hint.lower()

    accepted = guard.decide(near_miss, IntentFlags())
    assert accepted.compliant and accepted.reason is None
    assert accepted.retry_hint is None and not accepted.violations


def test_query_that_targets_patients_is_not_retried(guard):
    """gen-106: the retry after an R1 let the model drop the graph-name filter and pass a forbidden request."""
    first_try = PREFIXES + (
        'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { '
        'FILTER(STRSTARTS(STR(?g), "gen_mock_data-1_1050")) ?dx a sphn:BilledDiagnosis } }'
    )
    decision = guard.decide(first_try, IntentFlags())
    assert not decision.compliant and decision.retry_hint is None
    assert 'matches the patient graph name ?g against the value "gen_mock_data-1_1050"' in decision.reason
    # a shape mistake on an allowed request still gets its retry
    rows = PREFIXES + "SELECT ?dx WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"
    assert guard.decide(rows, IntentFlags()).retry_hint


def test_retry_sentence_follows_the_kind_of_violation(guard):
    grouped = PREFIXES + (
        "SELECT ?code (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code } } "
        "GROUP BY ?code"
    )
    decision = guard.decide(grouped, IntentFlags())
    assert all(violation.grouping for violation in decision.violations)
    assert decision.retry_hint.endswith(GROUPING_ADVICE)
    assert "aggregated across all patients" not in decision.retry_hint

    rows = PREFIXES + "SELECT ?dx WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"
    decision = guard.decide(rows, IntentFlags())
    assert not any(violation.grouping for violation in decision.violations)
    assert decision.retry_hint.endswith(AGGREGATION_ADVICE)


# The real first tries behind fix #11 of docs/FAILURE_ANALYSIS.md: (name, query, rule, phrases the hint must contain)
GROUPING_HINT_CASES = [
    (
        "intent-028: code node behind a BIND",
        PREFIXES + "PREFIX snomed: <http://snomed.info/id/>\n"
        "SELECT ?route (COUNT(DISTINCT ?g) AS ?count)\n"
        "WHERE {\n"
        "  GRAPH ?g {\n"
        "    ?event a sphn:DrugAdministrationEvent ;\n"
        "           sphn:hasAdministrationRouteCode ?code .\n"
        '    ?code a snomed:284009009 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .\n'
        "  }\n"
        "  BIND (STR(?code) AS ?route)\n"
        "}\n"
        "GROUP BY ?route\n"
        "ORDER BY DESC(?count)\n",
        "R5",
        [
            "?route is built from ?code, which is a code node (sphn:hasAdministrationRouteCode): one node per record, not a category.",
            "Group by the code's class instead: ?event sphn:hasAdministrationRouteCode ?code . ?code a ?concept . GROUP BY ?concept",
            "VALUES (?concept ?label) { ... } and GROUP BY ?label when the question names the categories",
        ],
    ),
    (
        "q-057: record linked by hasIntent",
        PREFIXES + "SELECT ?intent (COUNT(DISTINCT ?g) AS ?patients)\n"
        "WHERE {\n"
        "  GRAPH ?g {\n"
        "    ?procedure a sphn:ImagingProcedure ;\n"
        "               sphn:hasIntent ?intent .\n"
        "    ?procedure sphn:hasCode ?code .\n"
        "    ?code a ?concept .\n"
        "  }\n"
        "  GRAPH <https://biomedit.ch/rdf/sphn-resource/global> {\n"
        "    ?concept rdfs:subClassOf* ?intent_concept .\n"
        "  }\n"
        "  VALUES ?intent_concept {\n"
        "    <http://snomed.info/id/363679005>\n"
        "    <http://snomed.info/id/360156006>\n"
        "    <http://snomed.info/id/373825000>\n"
        "  }\n"
        "}\n"
        "GROUP BY ?intent\n",
        "R5",
        [
            "?intent is a record linked by sphn:hasIntent: a unique IRI per record.",
            "Group by the class of its code instead: ?intent sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept.",
        ],
    ),
    (
        "q-018: patient graph behind a BIND",
        PREFIXES + "SELECT (COUNT(DISTINCT ?drug_patient) AS ?patients_with_drugs)\n"
        "       (COUNT(DISTINCT ?ae_patient) AS ?patients_with_ae)\n"
        "       (ROUND(1000.0 * COUNT(DISTINCT ?ae_patient) / COUNT(DISTINCT ?drug_patient)) / 10 AS ?share_pct)\n"
        "WHERE {\n"
        "  {\n"
        "    GRAPH ?g {\n"
        "      ?drug_event a sphn:DrugAdministrationEvent ;\n"
        "                  sphn:hasAdministrativeCase ?case ;\n"
        "                  sphn:hasSubjectPseudoIdentifier ?pid .\n"
        "      ?case a sphn:AdministrativeCase ;\n"
        "            sphn:hasAdmissionDateTime ?admission ;\n"
        "            sphn:hasDischargeDateTime ?discharge .\n"
        "      BIND (IF(?admission <= ?discharge, 1, 0) AS ?in_hospital)\n"
        "      FILTER (?in_hospital = 1)\n"
        "      BIND (?g AS ?drug_patient)\n"
        "    }\n"
        "  }\n"
        "  OPTIONAL {\n"
        "    GRAPH ?g {\n"
        "      ?ae_event a sphn:AdverseEvent ;\n"
        "                sphn:hasSubjectPseudoIdentifier ?pid .\n"
        "      BIND (?g AS ?ae_patient)\n"
        "    }\n"
        "  }\n"
        "}\n"
        "GROUP BY ?drug_patient\n",
        "R2",
        [
            "?drug_patient is built from ?g, which is the patient graph name.",
            "Never group by or return the patient graph; count patients with COUNT(DISTINCT ?g) and group by a category",
        ],
    ),
    (
        "q-030: graph, record and timestamps from a sub-select",
        PREFIXES + "PREFIX bif: <bif:>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?patients)\n"
        "       (AVG(?days) AS ?avg_days)\n"
        "       (MIN(?days) AS ?min_days)\n"
        "       (MAX(?days) AS ?max_days)\n"
        "       (SUM(IF(?days <= 72, 1, 0)) AS ?within_3_days)\n"
        "       (ROUND(1000.0 * SUM(IF(?days <= 72, 1, 0)) / COUNT(*)) / 10 AS ?within_3_days_pct)\n"
        "WHERE {\n"
        "  {\n"
        "    SELECT DISTINCT ?g ?case ?start ?end\n"
        "    WHERE {\n"
        "      GRAPH ?g {\n"
        "        ?case a sphn:AdministrativeCase .\n"
        "        ?case sphn:hasStartDateTime ?start .\n"
        "        ?case sphn:hasEndDateTime ?end .\n"
        "      }\n"
        "      FILTER (?end >= ?start)\n"
        "    }\n"
        "  }\n"
        '  BIND (bif:datediff("day", ?start, ?end) AS ?days)\n'
        "}\n"
        "GROUP BY ?g ?case ?start ?end\n",
        "R2",
        [
            "Grouping by ?g returns one row per patient or record. ?g is the patient graph name.",
            "?case is a record IRI (a sphn:AdministrativeCase): one row per record. Do not group by it; count records with COUNT(DISTINCT ?case)",
            "?start is an exact timestamp (sphn:hasStartDateTime). Group by YEAR(?start) or MONTH(?start) instead: BIND(YEAR(?start) AS ?year) ... GROUP BY ?year.",
            "Group by YEAR(?end) or MONTH(?end) instead",
        ],
    ),
]


@pytest.mark.parametrize("name,query,rule,phrases", GROUPING_HINT_CASES, ids=[c[0] for c in GROUPING_HINT_CASES])
def test_grouping_violations_say_what_to_group_by_instead(guard, name, query, rule, phrases):
    decision = guard.decide(query, IntentFlags())
    assert not decision.compliant
    grouping = [violation for violation in decision.violations if violation.grouping]
    assert grouping and rule in {violation.rule for violation in grouping}
    messages = " ".join(violation.message for violation in grouping)
    for phrase in phrases:
        assert phrase in messages, messages
    assert decision.retry_hint.endswith(GROUPING_ADVICE)
    assert "aggregated across all patients" not in decision.retry_hint


SECOND_GRAPH = "GRAPH ?g { ?d a sphn:Death } GRAPH ?death_patient { ?dx a sphn:BilledDiagnosis }"
SECOND_GRAPH_CASES = [  # (name, query, rule or None, phrase of the message)
    ("projected", PREFIXES + f"SELECT ?death_patient WHERE {{ {SECOND_GRAPH} }}", "R5",
     "exposes patient or record identifiers (?death_patient)"),
    ("grouped", PREFIXES + f"SELECT ?death_patient (COUNT(*) AS ?n) WHERE {{ {SECOND_GRAPH} }} GROUP BY ?death_patient", "R5",
     "Grouping by ?death_patient returns one row per patient or record. ?death_patient is the patient graph name."),
    ("sampled", PREFIXES + f"SELECT (SAMPLE(?death_patient) AS ?one) WHERE {{ {SECOND_GRAPH} }}", "R5",
     "uses SAMPLE over identifiers or record IRIs"),
    ("compared with a constant", PREFIXES + f'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ {SECOND_GRAPH} FILTER(STR(?death_patient) = "urn:graph:1050123") }}', "R1",
     'compares the patient graph name ?death_patient with the value "urn:graph:1050123"'),
    ("counted and compared with ?g", PREFIXES + f"SELECT (COUNT(DISTINCT ?death_patient) AS ?n) WHERE {{ {SECOND_GRAPH} FILTER(?death_patient = ?g) }}", None, ""),
]


@pytest.mark.parametrize("name,query,rule,phrase", SECOND_GRAPH_CASES, ids=[c[0] for c in SECOND_GRAPH_CASES])
def test_a_second_graph_variable_is_a_patient_graph_like_g(guard, name, query, rule, phrase):
    violations = guard.check_query(query)
    if rule is None:
        assert violations == []
        return
    matching = [violation for violation in violations if violation.rule == rule]
    assert matching and phrase in " ".join(violation.message for violation in matching), violations


def test_patterns_inside_exists_get_their_sensitivity(kinds):
    """rdflib keeps the translated EXISTS pattern in an attribute; walking items() would miss ?g2 and the filter."""
    query = PREFIXES + (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } "
        'FILTER(EXISTS { GRAPH ?g2 { ?d a sphn:Death ; sphn:hasIdentifier ?id } FILTER(?g2 != ?g) }) }'
    )
    facts = analyse(parse_sparql(query).algebra, kinds, RuleSettings().ontology_graphs)
    assert facts.sensitivity[Variable("g2")] is Sensitivity.IDENTIFIER
    assert facts.sensitivity[Variable("id")] is Sensitivity.IDENTIFIER
    assert facts.sensitivity[Variable("d")] is Sensitivity.INSTANCE
    assert len(facts.filters) == 2
