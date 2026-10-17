"""R1 on expressions: anything that pins the patient graph name or an identifier to fixed values.

Ported from eval/analysis/test_guard_graph_variable.py (fix #13 of docs/FAILURE_ANALYSIS.md).
"""
import pytest

from conftest import PREFIXES
from nl2sparql.models import IntentFlags

P = PREFIXES + "PREFIX bif: <bif:>\n"
COUNT = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } "
PREFIX_1050 = "http://example.org/graph/gen_mock_data-1_1050"

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
    ("comparison in ORDER BY", P + COUNT + '} ORDER BY DESC(STRSTARTS(STR(?g), "' + PREFIX_1050 + '"))'),
    ("comparison in a GROUP BY expression", P + 'SELECT ?k (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY (STRSTARTS(STR(?g), "' + PREFIX_1050 + '") AS ?k)'),
    ("graph IN a list", P + COUNT + "FILTER(?g IN (<urn:graph:1050123>, <urn:graph:1050124>)) }"),
    ("graph NOT IN a list", P + COUNT + "FILTER(?g NOT IN (<urn:graph:1050123>)) }"),
    ("sameTerm on the graph", P + COUNT + "FILTER(sameTerm(?g, <urn:graph:1050123>)) }"),
    ("VALUES on an identifier literal", P + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { VALUES ?id { "ID-10125020000015" } GRAPH ?g { ?s sphn:hasIdentifier ?id } }'),
    ("VALUES on a pseudo-identifier", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { VALUES ?pid { <urn:pid:1> } GRAPH ?g { ?s sphn:hasSubjectPseudoIdentifier ?pid } }"),
    ("second graph variable matched", P + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d a sphn:Death } GRAPH ?death_patient { ?dx a sphn:BilledDiagnosis } FILTER(STRSTARTS(STR(?death_patient), "' + PREFIX_1050 + '")) }'),
    ("graph variable pinned inside EXISTS", P + COUNT + 'FILTER(EXISTS { GRAPH ?g2 { ?d a sphn:Death } FILTER(STR(?g2) = "urn:graph:1050123") }) }'),
    ("prefix with digits inside NOT EXISTS", P + COUNT + 'FILTER NOT EXISTS { GRAPH ?g { ?d a sphn:Death } FILTER(STRSTARTS(STR(?g), "gen_mock_data-1_1050")) } }'),
]

GRAPH_VARIABLE_ALLOWED = [  # (name, query): every one must stay compliant
    ("bare patient-graph prefix", P + COUNT + 'FILTER(STRSTARTS(STR(?g), "http://example.org/graph/")) }'),
    ("bare prefix in a custom function", P + COUNT + 'FILTER(bif:starts_with(STR(?g), "http://example.org/graph/")) }'),
    ("bare prefix in an anchored REGEX", P + COUNT + 'FILTER(REGEX(STR(?g), "^http://example.org/graph/")) }'),
    ("two graphs compared with each other", P + COUNT + "GRAPH ?g2 { ?dx2 a sphn:BilledDiagnosis } FILTER(?g != ?g2) }"),
    ("BOUND on a renamed graph", P + COUNT + "OPTIONAL { GRAPH ?g { ?e a sphn:AdverseEvent } BIND(?g AS ?ae) } FILTER(BOUND(?ae)) }"),
    ("IF picks the graph or a constant", P + "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } OPTIONAL { GRAPH ?g { ?e a sphn:AdverseEvent } } BIND(IF(BOUND(?e), ?g, 0) AS ?p) }"),
    ("COALESCE of two renamed graphs", P + "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { { GRAPH ?g { ?dx a sphn:BilledDiagnosis } BIND(?g AS ?a) } UNION { GRAPH ?g { ?e a sphn:AdverseEvent } BIND(?g AS ?b) } BIND(COALESCE(?a, ?b) AS ?p) }"),
    ("HAVING on a patient count", P + "SELECT ?rank (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx sphn:hasRank ?rank } } GROUP BY ?rank HAVING (COUNT(DISTINCT ?g) >= 5)"),
    ("prefix filter on a concept (official style)", P + 'SELECT ?concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx sphn:hasCode ?c . ?c a ?concept } FILTER(STRSTARTS(STR(?concept), "http://snomed.info/id/")) } GROUP BY ?concept'),
    ("value and date filters", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?q sphn:hasValue ?v . ?dx sphn:hasRecordDateTime ?d } FILTER(?v > 40 && YEAR(?d) >= 2015) }"),
    ("second graph variable counted", P + "PREFIX sphn-individual: <https://biomedit.ch/rdf/sphn-schema/sphn/individual#>\nSELECT ?severity (COUNT(DISTINCT ?g) AS ?total) (COUNT(DISTINCT ?death_patient) AS ?deaths) WHERE { GRAPH ?g { ?event a sphn:AdverseEvent ; sphn:hasSeverityCode ?code . ?code a ?severity } OPTIONAL { GRAPH ?death_patient { ?event sphn:hasOutcome sphn-individual:Death } } } GROUP BY ?severity"),
    ("house-style NOT EXISTS", P + COUNT + "FILTER NOT EXISTS { GRAPH ?g { ?d a sphn:Death } } }"),
    ("bare prefix inside EXISTS", P + COUNT + 'FILTER EXISTS { GRAPH ?g2 { ?d a sphn:Death } FILTER(?g2 = ?g && STRSTARTS(STR(?g2), "http://example.org/graph/")) } }'),
]


@pytest.mark.parametrize("name,query", GRAPH_VARIABLE_PINNED, ids=[c[0] for c in GRAPH_VARIABLE_PINNED])
def test_expressions_that_pin_the_graph_variable_break_r1(guard, name, query):
    decision = guard.decide(query, IntentFlags())
    assert not decision.compliant
    assert "R1" in {violation.rule for violation in decision.violations}, decision.violations
    assert decision.retry_hint is None  # the values came from the request: never retried


@pytest.mark.parametrize("name,query", GRAPH_VARIABLE_ALLOWED, ids=[c[0] for c in GRAPH_VARIABLE_ALLOWED])
def test_house_style_uses_of_the_graph_variable_stay_compliant(guard, name, query):
    assert guard.check_query(query) == []


MESSAGES = [
    ("graph matched against a literal", P + COUNT + 'FILTER(STRSTARTS(STR(?g), "gen_mock_data-1_1050")) }',
     'The query matches the patient graph name ?g against the value "gen_mock_data-1_1050".'),
    ("graph compared with constants", P + COUNT + "FILTER(?g IN (<urn:graph:1050123>, <urn:graph:1050124>)) }",
     'The query compares the patient graph name ?g with the values "urn:graph:1050123", "urn:graph:1050124".'),
    ("graph matched against an expression", P + COUNT + 'FILTER(STRSTARTS(STR(?g), CONCAT("http://example.org/graph/gen_mock_", "data-1_1050"))) }',
     "The query matches the patient graph name ?g against a value that is not an identifier."),
    ("identifier compared with a value", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s sphn:hasIdentifier ?id . ?q sphn:hasValue ?v } FILTER(?id = STR(?v)) }",
     "The query compares the identifier ?id with a value that is not an identifier."),
    ("VALUES on the graph variable", P + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { VALUES ?g { <urn:graph:1050123> } GRAPH ?g { ?dx a sphn:BilledDiagnosis } }",
     "VALUES pins the patient graph name ?g to fixed values."),
    ("constant bound into the graph variable", P + 'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { BIND(IRI("urn:graph:1050123") AS ?g) GRAPH ?g { ?dx a sphn:BilledDiagnosis } }',
     "BIND assigns a fixed value to the patient graph name ?g."),
]


@pytest.mark.parametrize("name,query,message", MESSAGES, ids=[c[0] for c in MESSAGES])
def test_pinning_messages_name_the_variable_and_the_values(guard, name, query, message):
    assert message in [violation.message for violation in guard.check_query(query) if violation.rule == "R1"]


def test_one_message_per_pinning_expression(guard):
    """The old comparison check and the new one must not report the same FILTER twice."""
    query = P + COUNT + 'FILTER(STR(?g) = "http://example.org/graph/gen_mock_data-1_1012502") }'
    violations = guard.check_query(query)
    assert [violation.rule for violation in violations] == ["R1"]
    assert violations[0].message == (
        'The query compares the patient graph name ?g with the value "http://example.org/graph/gen_mock_data-1_1012502".'
    )
