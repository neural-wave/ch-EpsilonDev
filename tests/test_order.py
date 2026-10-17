from nl2sparql.sparql.order import DEFINE_ORDER, reorder
from nl2sparql.sparql.parse import parse_sparql

INFERENCE = 'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")'
PREFIXES = "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nPREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"


def test_moves_the_code_pattern_first_and_asks_virtuoso_to_keep_the_order():
    query = PREFIXES + f"""SELECT (COUNT(DISTINCT ?g) AS ?patients)
WHERE {{
  GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis ;
        sphn:hasCode ?dx_code .
    ?dx_code a icd-10-gm:E11 {INFERENCE} .
  }}
}}"""
    out = reorder(query)
    assert out.startswith(DEFINE_ORDER + "\n")
    assert out.index("?dx_code a icd-10-gm:E11") < out.index("?dx a sphn:BilledDiagnosis")
    assert out.count("?dx_code") == 2 and out.endswith("}\n}")
    assert parse_sparql(out).kind == "SelectQuery"  # still valid once the Virtuoso extensions are stripped


def test_keeps_optional_filter_and_bind_after_the_triples_in_their_order():
    query = PREFIXES + f"""SELECT ?g WHERE {{
  GRAPH ?g {{
    FILTER (?admit <= ?discharge)
    ?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?admit ; sphn:hasDischargeDateTime ?discharge .
    OPTIONAL {{ ?x sphn:hasCode ?c2 . ?c2 a icd-10-gm:A00.1 {INFERENCE} . }}
    BIND (?g AS ?p)
    ?dx sphn:hasCode ?c .
    ?c a icd-10-gm:A00 {INFERENCE} .
  }}
}}"""
    out = reorder(query)
    order = [line.strip()[:12] for line in out.splitlines()
             if line.strip() and "GRAPH" not in line and not line.strip().startswith(("PREFIX", "SELECT", "DEFINE", "}"))]
    assert order[0].startswith("?c a icd-10") and order[1].startswith("?dx sphn:has")
    assert order[2:] == ["FILTER (?adm", "?case a sphn", "OPTIONAL { ?", "BIND (?g AS "]
    assert "A00.1" in out  # a dot inside a code is not a statement end


def test_leaves_queries_without_a_code_pattern_or_already_ordered_untouched():
    plain = PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?x a sphn:AdministrativeSex } }"
    assert reorder(plain) == plain
    ordered = PREFIXES + f"SELECT ?g WHERE {{ GRAPH ?g {{ ?c a icd-10-gm:A00 {INFERENCE} . ?dx sphn:hasCode ?c . }} }}"
    out = reorder(ordered)
    assert out.startswith(DEFINE_ORDER) and out.replace(DEFINE_ORDER + "\n", "") == ordered
    assert reorder(out) == out  # idempotent
    assert reorder("SELECT ?g WHERE { GRAPH ?g { ?c a icd-10-gm:A00 . ") == "SELECT ?g WHERE { GRAPH ?g { ?c a icd-10-gm:A00 . "  # unbalanced: untouched
