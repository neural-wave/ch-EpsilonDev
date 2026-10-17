from nl2sparql.sparql.order import DEFINE_ORDER, reorder
from nl2sparql.sparql.parse import parse_sparql

INFERENCE = 'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")'
PREFIXES = ("PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
            "PREFIX sphn-individual: <https://biomedit.ch/rdf/sphn-schema/sphn/individual#>\n"
            "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
            "PREFIX snomed: <http://snomed.info/id/>\n")
Q015 = PREFIXES + f"""SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(?dx) AS ?diagnoses)
WHERE {{
  GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis ;
        sphn:hasRank ?rank ;
        sphn:hasRecordDateTime ?date ;
        sphn:hasAdministrativeCase ?case ;
        sphn:hasSubjectPseudoIdentifier ?pid ;
        sphn:hasCode ?dx_code .
    ?dx_code a icd-10-gm:A00 {INFERENCE} .
    FILTER (YEAR(?date) >= 2010)
  }}
}}"""
SPEC01 = PREFIXES + f"""SELECT (COUNT(DISTINCT ?g) AS ?patients)
WHERE {{
  GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis ;
        sphn:hasCode ?dx_code .
    ?dx_code a icd-10-gm:E11 {INFERENCE} .
  }}
}}"""


def body_lines(sparql: str) -> list[str]:
    """The statements of the query body, one per line, without the frame around them."""
    skip = ("PREFIX", "SELECT", "DEFINE", "WHERE", "}", "GRAPH")
    return [line.strip() for line in sparql.splitlines() if line.strip() and not line.strip().startswith(skip)]


def triples(sparql: str) -> set[tuple]:
    """Every triple of the query's algebra, wherever it sits: the rewrite may only change their order."""
    found = set()

    def walk(node):
        if hasattr(node, "name") and node.name == "BGP":
            found.update(tuple(triple) for triple in node.triples)
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
    walk(parse_sparql(sparql).algebra)
    return found


def test_rotates_the_linking_statement_so_the_code_lookup_comes_first():
    out = reorder(Q015)
    assert out.startswith(DEFINE_ORDER + "\n")
    assert body_lines(out) == [
        f"?dx_code a icd-10-gm:A00 {INFERENCE} .",
        "?dx sphn:hasCode ?dx_code ;",
        "a sphn:BilledDiagnosis ;",
        "sphn:hasRank ?rank ;",
        "sphn:hasRecordDateTime ?date ;",
        "sphn:hasAdministrativeCase ?case ;",
        "sphn:hasSubjectPseudoIdentifier ?pid .",
        "FILTER (YEAR(?date) >= 2010)",
    ]
    assert "    ?dx sphn:hasCode ?dx_code ;\n        a sphn:BilledDiagnosis ;" in out  # aligned under the first predicate
    assert parse_sparql(out).kind == "SelectQuery" and triples(out) == triples(Q015)


def test_spec01_shape_becomes_code_first_with_the_define():
    out = reorder(SPEC01)
    assert out.startswith(DEFINE_ORDER + "\n")
    assert body_lines(out) == [f"?dx_code a icd-10-gm:E11 {INFERENCE} .", "?dx sphn:hasCode ?dx_code ;", "a sphn:BilledDiagnosis ."]
    assert triples(out) == triples(SPEC01) and out.endswith("}\n}")


def test_full_iri_code_and_rdf_type_count_as_a_typed_code_pattern():
    query = PREFIXES + ("SELECT ?g WHERE { GRAPH ?g { ?dx rdf:type sphn:BilledDiagnosis ; sphn:hasCode ?c . "
                        "?c rdf:type <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/E11> . } }")
    out = reorder(query)
    assert out.startswith(DEFINE_ORDER)
    assert out.index("?c rdf:type <https://") < out.index("?dx sphn:hasCode ?c ;\n") < out.index("rdf:type sphn:BilledDiagnosis .")


def test_already_ordered_query_only_gets_the_define_and_the_pass_is_idempotent():
    ordered = PREFIXES + f"SELECT ?g WHERE {{ GRAPH ?g {{ ?c a icd-10-gm:A00 {INFERENCE} . ?dx sphn:hasCode ?c ; a sphn:BilledDiagnosis . }} }}"
    out = reorder(ordered)
    assert out == DEFINE_ORDER + "\n" + ordered
    assert reorder(out) == out
    assert reorder(reorder(Q015)) == reorder(Q015)


def test_a_last_statement_without_a_terminator_gets_one_when_it_moves():
    query = PREFIXES + f"SELECT ?g WHERE {{ GRAPH ?g {{ ?dx sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} }} }}"
    out = reorder(query)
    assert f"?c a icd-10-gm:A00 {INFERENCE} .\n ?dx sphn:hasCode ?c ." in out
    assert parse_sparql(out).kind == "SelectQuery"
    literal_last = PREFIXES + ('SELECT ?g WHERE { GRAPH ?g { ?c a icd-10-gm:A00 . '
                               '?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c ; rdfs:comment "cholera; A00." } }')
    out = reorder(literal_last)
    assert '?dx sphn:hasCode ?c ;\n     a sphn:BilledDiagnosis ;\n     rdfs:comment "cholera; A00." .' in out
    assert parse_sparql(out).kind == "SelectQuery" and triples(out) == triples(literal_last)


def test_code_and_link_move_past_filter_values_and_unrelated_bind_and_optional():
    query = PREFIXES + f"""SELECT ?g WHERE {{
  GRAPH ?g {{
    FILTER (?admit <= ?discharge)
    VALUES ?rank {{ sphn-individual:Principal sphn-individual:Secondary }}
    ?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?admit ; sphn:hasDischargeDateTime ?discharge .
    OPTIONAL {{ ?death a sphn:Death ; sphn:hasDate ?discharge . }}
    BIND (?g AS ?p)
    ?dx a sphn:BilledDiagnosis ; sphn:hasAdministrativeCase ?case ; sphn:hasRank ?rank ; sphn:hasCode ?c .
    ?c a icd-10-gm:A00.1 {INFERENCE} .
  }}
}}"""
    out = reorder(query)
    assert [line[:14] for line in body_lines(out)] == [
        "?c a icd-10-gm", "?dx sphn:hasCo", "a sphn:BilledD", "sphn:hasAdmini", "sphn:hasRank ?",
        "FILTER (?admit", "VALUES ?rank {", "?case a sphn:A", "OPTIONAL { ?de", "BIND (?g AS ?p",
    ]
    assert "A00.1" in out and triples(out) == triples(query)  # a dot inside a code is not a statement end


def test_untouched_when_the_code_or_link_would_cross_a_bind_or_optional_that_shares_a_variable():
    bind_first = PREFIXES + f"""SELECT ?g WHERE {{ GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis . BIND (?dx AS ?record)
    ?dx sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }} }}"""
    assert reorder(bind_first) == bind_first
    optional_first = PREFIXES + f"""SELECT ?g WHERE {{ GRAPH ?g {{
    OPTIONAL {{ ?dx sphn:hasRank ?rank . }}
    ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }} }}"""
    assert reorder(optional_first) == optional_first


def test_two_code_patterns_are_left_alone():
    query = PREFIXES + f"""SELECT ?g WHERE {{ GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} .
    ?proc a sphn:BilledProcedure ; sphn:hasCode ?pc . ?pc a snomed:33195004 {INFERENCE} . }} }}"""
    assert reorder(query) == query


def test_union_minus_and_sub_select_are_left_alone():
    union = PREFIXES + f"""SELECT ?g WHERE {{
  {{ GRAPH ?g {{ ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }} }}
  UNION {{ GRAPH ?g {{ ?x a sphn:OncologyDiagnosis . }} }} }}"""
    assert reorder(union) == union
    minus = PREFIXES + f"""SELECT ?g WHERE {{ GRAPH ?g {{ ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }}
  MINUS {{ GRAPH ?g {{ ?x a sphn:Death . }} }} }}"""
    assert reorder(minus) == minus
    sub_select = PREFIXES + f"""SELECT (COUNT(?p) AS ?n) WHERE {{
  {{ SELECT DISTINCT (?g AS ?p) WHERE {{ GRAPH ?g {{ ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }} }} }} }}"""
    assert reorder(sub_select) == sub_select


def test_inference_inside_an_optional_or_on_another_statement_is_left_alone():
    optional = PREFIXES + f"""SELECT ?g WHERE {{
  GRAPH ?g {{ ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }}
  OPTIONAL {{ GRAPH ?g {{ ?x sphn:hasCode ?c2 . ?c2 a ?concept {INFERENCE} . }} }} }}"""
    assert reorder(optional) == optional
    later_block = PREFIXES + f"""SELECT ?g WHERE {{
  GRAPH ?g {{ ?sex a sphn:AdministrativeSex . }}
  OPTIONAL {{ GRAPH ?g {{ ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} . }} BIND (?g AS ?cholera) }} }}"""
    assert reorder(later_block) == later_block


def test_a_statement_that_joins_nothing_before_it_is_left_alone():
    query = PREFIXES + f"""SELECT ?g WHERE {{ GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:A00 {INFERENCE} .
    ?sex a sphn:AdministrativeSex ; sphn:hasCode ?sex_code . }} }}"""
    assert reorder(query) == query
    linked = query.replace("?sex a sphn:AdministrativeSex ;", "?sex a sphn:AdministrativeSex ; sphn:hasSubjectPseudoIdentifier ?pid ;")
    linked = linked.replace("sphn:hasCode ?c .", "sphn:hasSubjectPseudoIdentifier ?pid ; sphn:hasCode ?c .", 1)
    assert reorder(linked).startswith(DEFINE_ORDER)


def test_queries_without_a_code_pattern_with_the_define_or_unbalanced_are_untouched():
    plain = PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?x a sphn:AdministrativeSex } }"
    assert reorder(plain) == plain
    assert reorder(DEFINE_ORDER + "\n" + Q015) == DEFINE_ORDER + "\n" + Q015  # the model wrote the DEFINE itself
    unbalanced = "SELECT ?g WHERE { GRAPH ?g { ?dx sphn:hasCode ?c . ?c a icd-10-gm:A00 . "
    assert reorder(unbalanced) == unbalanced
    assert reorder(unbalanced + "} }} }") == unbalanced + "} }} }"
