from rdflib import URIRef

from nl2sparql.sparql.parse import parse_sparql, strip_virtuoso_extensions
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes
from nl2sparql.sparql.terms import collect_iris

OFFICIAL_EXAMPLE = """DEFINE sql:select-option "order"
PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT (COUNT(?pseudo_id) AS ?countResult)
WHERE {
  { SELECT DISTINCT(?g AS ?pseudo_id)
    WHERE { GRAPH ?g {
      ?diagnosis0_code rdf:type <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/E11>
          OPTION(inference "https://biomedit.ch/rdf/sphn-resource/global") .
      ?diagnosis sphn:hasCode ?diagnosis0_code .
      ?diagnosis rdf:type sphn:BilledDiagnosis .
    } } }
}"""


def test_strips_virtuoso_extensions():
    stripped, defines, inference_count = strip_virtuoso_extensions(OFFICIAL_EXAMPLE)
    assert "DEFINE" not in stripped and "OPTION" not in stripped
    assert defines == ['DEFINE sql:select-option "order"']
    assert inference_count == 1


def test_parses_official_example_as_select():
    parsed = parse_sparql(OFFICIAL_EXAMPLE)
    assert parsed.kind == "SelectQuery"
    assert parsed.inference_options == 1
    assert parsed.defines == ['DEFINE sql:select-option "order"']
    standard = parse_sparql(
        f"PREFIX sphn: <{SPHN}> SELECT (COUNT(DISTINCT ?g) AS ?n) "
        "WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis } }"
    )
    assert standard.kind == "SelectQuery"
    assert standard.defines == [] and standard.inference_options == 0


def test_detects_update_and_other_forms():
    assert parse_sparql("PREFIX sphn: <%s>\nDELETE WHERE { ?s a sphn:BilledDiagnosis }" % SPHN).kind == "Update"
    assert parse_sparql("ASK { ?s ?p ?o }").kind == "AskQuery"
    assert parse_sparql("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }").kind == "ConstructQuery"


def test_reports_syntax_errors():
    parsed = parse_sparql("SELECT ?x WHERE { ?x ")
    assert parsed.kind == "Invalid" and parsed.error


def test_adds_missing_prefixes_after_defines():
    query = 'DEFINE sql:select-option "order"\nSELECT (COUNT(*) AS ?n) WHERE { ?d a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 }'
    fixed = add_missing_prefixes(query)
    assert fixed.startswith('DEFINE sql:select-option "order"\nPREFIX sphn: <')
    assert "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>" in fixed
    assert "PREFIX sql" not in fixed
    assert parse_sparql(fixed).kind == "SelectQuery"


def test_does_not_redeclare_or_touch_iris():
    query = f'PREFIX sphn: <{SPHN}>\nSELECT * WHERE {{ ?s <http://x.org/a:b> "rdf:not-a-prefix" ; sphn:hasCode ?c }}'
    assert add_missing_prefixes(query) == query


def test_collects_iris_including_paths():
    parsed = parse_sparql(
        f"PREFIX sphn: <{SPHN}>\nSELECT (AVG(?v) AS ?a) WHERE {{ ?x a sphn:Age ; sphn:hasQuantity/sphn:hasValue ?v }}"
    )
    iris = collect_iris(parsed.algebra)
    assert URIRef(SPHN + "Age") in iris
    assert URIRef(SPHN + "hasQuantity") in iris and URIRef(SPHN + "hasValue") in iris
