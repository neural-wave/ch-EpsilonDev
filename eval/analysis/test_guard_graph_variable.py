"""Proposed tests for tests/test_guard.py: expressions that pin the patient graph name or an identifier.

Run here against the scratch patch; in the repo, drop the `patched_guard` fixture and use `guard`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path("/Users/santiagofigueroa/Desktop/Hackathon/nl2sparql")
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import DATA_DIR, PREFIXES  # noqa: E402
from guard_rule_patch import check_rules_patched  # noqa: E402
from nl2sparql.guard import ComplianceGuard, PropertyKinds  # noqa: E402
from nl2sparql.models import IntentFlags  # noqa: E402
from nl2sparql.sparql.parse import parse_sparql  # noqa: E402

P = PREFIXES + "PREFIX bif: <bif:>\n"
COUNT = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } "
PREFIX_1050 = "http://example.org/graph/gen_mock_data-1_1050"


@pytest.fixture(scope="session")
def patched_guard() -> ComplianceGuard:
    class PatchedGuard(ComplianceGuard):
        def check_query(self, sparql: str):
            return check_rules_patched(parse_sparql(sparql), self.kinds, self.settings)

    return PatchedGuard(PropertyKinds.load(DATA_DIR / "property_kinds.json"))


GRAPH_VARIABLE_PINNED = [  # (name, query): every one must break R1
    ("prefix in a VALUES variable", P + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { VALUES ?prefix { "' + PREFIX_1050 + '" } GRAPH ?g { ?dx a sphn:BilledDiagnosis } FILTER(STRSTARTS(STR(?g), ?prefix)) }'),
    ("prefix built with CONCAT", P + COUNT + 'FILTER(STRSTARTS(STR(?g), CONCAT("http://example.org/graph/gen_mock_", "data-1_1050"))) }'),
    ("graph compared with IRI()", P + COUNT + 'FILTER(?g = IRI("' + PREFIX_1050 + '123")) }'),
    ("graph string compared with CONCAT", P + COUNT + 'FILTER(STR(?g) = CONCAT("http://example.org/graph/", "gen_mock_data-1_1050123")) }'),
    ("virtuoso function on the graph", P + COUNT + 'FILTER(bif:starts_with(STR(?g), "' + PREFIX_1050 + '")) }'),
    ("comparison inside BIND", P + 'SELECT (SUM(?flag) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } BIND(IF(STRSTARTS(STR(?g), "' + PREFIX_1050 + '"), 1, 0) AS ?flag) }'),
    ("comparison inside an aggregate", P + 'SELECT (SUM(IF(STRSTARTS(STR(?g), "' + PREFIX_1050 + '"), 1, 0)) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }'),
    ("length of the graph name", P + COUNT + "FILTER(STRLEN(STR(?g)) = 45) }"),
    ("VALUES on the graph variable", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { VALUES ?g { <urn:graph:1050123> } GRAPH ?g { ?dx a sphn:BilledDiagnosis } }"),
    ("constant bound into the graph variable", P + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { BIND(IRI("urn:graph:1050123") AS ?g) GRAPH ?g { ?dx a sphn:BilledDiagnosis } }'),
    ("identifier compared with a value", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id . ?q sphn:hasValue ?v } FILTER(?id = STR(?v)) }"),
    ("prefix literal with digits (gen-106)", P + COUNT + 'FILTER(STRSTARTS(STR(?g), "gen_mock_data-1_1050")) }'),
]

GRAPH_VARIABLE_ALLOWED = [  # (name, query): every one must stay compliant
    ("bare patient-graph prefix", P + COUNT + 'FILTER(STRSTARTS(STR(?g), "http://example.org/graph/")) }'),
    ("two graphs compared with each other", P + COUNT + "GRAPH ?g2 { ?dx2 a sphn:BilledDiagnosis } FILTER(?g != ?g2) }"),
    ("BOUND on a renamed graph", P + COUNT + "OPTIONAL { GRAPH ?g { ?e a sphn:AdverseEvent } BIND(?g AS ?ae) } FILTER(BOUND(?ae)) }"),
    ("IF picks the graph or a constant", P + "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } OPTIONAL { GRAPH ?g { ?e a sphn:AdverseEvent } } BIND(IF(BOUND(?e), ?g, 0) AS ?p) }"),
    ("COALESCE of two renamed graphs", P + "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { { GRAPH ?g { ?dx a sphn:BilledDiagnosis } BIND(?g AS ?a) } UNION { GRAPH ?g { ?e a sphn:AdverseEvent } BIND(?g AS ?b) } BIND(COALESCE(?a, ?b) AS ?p) }"),
    ("HAVING on a patient count", P + "SELECT ?rank (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx sphn:hasRank ?rank } } GROUP BY ?rank HAVING (COUNT(DISTINCT ?g) >= 5)"),
    ("prefix filter on a concept (official style)", P + 'SELECT ?concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx sphn:hasCode ?c . ?c a ?concept } FILTER(STRSTARTS(STR(?concept), "http://snomed.info/id/")) } GROUP BY ?concept'),
    ("value and date filters", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?q sphn:hasValue ?v . ?dx sphn:hasRecordDateTime ?d } FILTER(?v > 40 && YEAR(?d) >= 2015) }"),
]


@pytest.mark.parametrize("name,query", GRAPH_VARIABLE_PINNED, ids=[c[0] for c in GRAPH_VARIABLE_PINNED])
def test_expressions_that_pin_the_graph_variable_break_r1(patched_guard, name, query):
    decision = patched_guard.decide(query, IntentFlags())
    assert not decision.compliant
    assert "R1" in {violation.rule for violation in decision.violations}, decision.violations


@pytest.mark.parametrize("name,query", GRAPH_VARIABLE_ALLOWED, ids=[c[0] for c in GRAPH_VARIABLE_ALLOWED])
def test_house_style_uses_of_the_graph_variable_stay_compliant(patched_guard, name, query):
    assert patched_guard.check_query(query) == []
