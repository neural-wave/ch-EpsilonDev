from rdflib import URIRef

from nl2sparql.sparql.parse import parse_sparql, strip_virtuoso_extensions
from nl2sparql.sparql.prefixes import SPHN, STANDARD_PREFIXES, add_missing_prefixes, fix_standard_prefixes
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


def test_rewrites_standard_prefixes_declared_with_another_namespace():
    # Seen in generated queries: the OBO namespace for icd-10-gm, geno under biomedit, a made-up
    # sphn-individual IRI and SNOMED's "sid" path. Every term under them then names nothing in the data.
    query = (
        "PREFIX icd-10-gm: <http://purl.obolibrary.org/obo/ICD10GM_>\n"
        "PREFIX geno: <https://biomedit.ch/rdf/sphn-resource/geno/>\n"
        "PREFIX sphn-individual: <https://biomedit.ch/rdf/sphn-individual/>\n"
        "prefix snomed: <http://snomed.info/sid/>\n"
        "SELECT * WHERE { ?d sphn:hasRank sphn-individual:Principal ; sphn:hasCode ?c . ?c a icd-10-gm:E11 }"
    )
    fixed = fix_standard_prefixes(query)
    for name in ("icd-10-gm", "geno", "sphn-individual"):
        assert f"PREFIX {name}: <{STANDARD_PREFIXES[name]}>\n" in fixed
    assert "prefix snomed: <http://snomed.info/id/>\n" in fixed
    assert fixed.splitlines()[-1] == query.splitlines()[-1]
    parsed = parse_sparql(add_missing_prefixes(fixed))
    assert parsed.kind == "SelectQuery"
    assert URIRef(STANDARD_PREFIXES["icd-10-gm"] + "E11") in collect_iris(parsed.algebra)
    assert URIRef(STANDARD_PREFIXES["sphn-individual"] + "Principal") in collect_iris(parsed.algebra)


def test_keeps_bif_other_prefix_names_and_right_declarations():
    query = (
        "PREFIX bif: <http://www.openlinksw.com/schemas/bif#>\n"
        f"PREFIX icd: <{STANDARD_PREFIXES['icd-10-gm']}>\n"
        f"PREFIX sphn: <{SPHN}>\n"
        'SELECT * WHERE { ?c a icd:E11 ; sphn:hasValue "PREFIX snomed: <http://snomed.info/sid/>" }'
    )
    assert fix_standard_prefixes(query) == query


def test_collects_iris_including_paths():
    parsed = parse_sparql(
        f"PREFIX sphn: <{SPHN}>\nSELECT (AVG(?v) AS ?a) WHERE {{ ?x a sphn:Age ; sphn:hasQuantity/sphn:hasValue ?v }}"
    )
    iris = collect_iris(parsed.algebra)
    assert URIRef(SPHN + "Age") in iris
    assert URIRef(SPHN + "hasQuantity") in iris and URIRef(SPHN + "hasValue") in iris


def test_parsing_from_many_threads_at_once_stays_correct():
    # Regression: rdflib's parser is not thread-safe and the orchestrator parses in worker threads.
    # Without the lock in parse_sparql, concurrent parses corrupt the grammar and every later parse fails.
    from concurrent.futures import ThreadPoolExecutor

    from conftest import load_examples

    queries = [example["sparql"] for example in load_examples() if example["compliant"]] * 10
    with ThreadPoolExecutor(max_workers=8) as pool:
        kinds = list(pool.map(lambda query: parse_sparql(query).kind, queries))
    assert set(kinds) == {"SelectQuery"}
    assert parse_sparql("SELECT ?x WHERE { ?x ?p ?o }").kind == "SelectQuery"
