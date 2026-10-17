import json

import pytest
from rdflib import Variable

from conftest import DATA_DIR, PREFIXES, load_examples
from nl2sparql.context.schema import SchemaCards
from nl2sparql.judge_checks.rewrites_scope import (
    CHECKS,
    REWRITES,
    check_bind_out_of_scope,
    check_nested_comparison,
    check_path_in_expression,
    check_unbound_count_records,
    record_subgroup_hint,
    rewrite_count_of_misnamed_record,
    rewrite_hidden_sub_select_variables,
    rewrite_nested_comparison,
    rewrite_path_in_filter,
    rewrite_record_link_typed_with_code,
    rewrite_values_on_code_node,
)
from nl2sparql.judge_checks.scope import _find, check_unbound_variables
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes

EVAL_DIR = DATA_DIR.parent / "eval"
ROOT = "https://biomedit.ch/rdf/sphn-resource/global"
OPTION = f'OPTION (inference "{ROOT}")'
SEX = (
    "GRAPH ?g { ?sex_record a sphn:AdministrativeSex ; sphn:hasCode ?sex_code . ?sex_code a ?sex_concept } "
    'VALUES (?sex_concept ?sex) { (snomed:248153007 "male") (snomed:248152002 "female") } '
)


def prepare(query: str):
    """The query as the judge hands it to a rewrite or a check: prefixes added, parsed."""
    fixed = add_missing_prefixes(PREFIXES + query.strip() + "\n")
    parsed = parse_sparql(fixed)
    assert parsed.kind == "SelectQuery", parsed.error
    return fixed, parsed


def rewrite(function, query: str, **keywords):
    fixed, parsed = prepare(query)
    rewritten, repairs = function(fixed, parsed, **keywords)
    if rewritten != fixed:
        assert parse_sparql(rewritten).kind == "SelectQuery"  # a rewrite must leave the query parseable
    return rewritten[len(PREFIXES):] if rewritten.startswith(PREFIXES) else rewritten, repairs


def hints(check, query: str) -> list[str]:
    fixed, parsed = prepare(query)
    return check(parsed, fixed)


def body(text: str) -> str:
    """The query without its PREFIX lines, with whitespace folded, for assertions on the rewritten text."""
    return " ".join(line for line in text.splitlines() if not line.startswith("PREFIX")).split()


def load_dev_references() -> list[tuple[str, str]]:
    """The team examples and the dev reference queries; holdout rows are a blind exam and stay closed."""
    queries = []
    for line in (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            queries.append((row["id"], row["sparql"]))
    for line in (EVAL_DIR / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("split") == "dev":
                queries.append((row["id"], row["reference_sparql"]))
    return queries


@pytest.fixture(scope="module")
def schema() -> SchemaCards:
    return SchemaCards.load(DATA_DIR / "schema_cards.json")


# ---------------------------------------------------------------- hidden sub-select variables (R3)

FOLLOW_UPS = (  # q-073: the group label and the record are bound inside the sub-select and read outside it
    "SELECT ?group_name (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?follow_up) AS ?follow_ups) WHERE { "
    "{ SELECT ?g (COUNT(DISTINCT ?follow_up) AS ?fu_count) WHERE { GRAPH ?g { ?follow_up a sphn:FollowUp ; "
    "sphn:hasCode ?c . ?c a ?concept } VALUES (?concept ?group_name) { (snomed:438949009 \"Alive\") } } "
    "GROUP BY ?g ?fu_count } } GROUP BY ?group_name ORDER BY ?group_name"
)


def test_hidden_group_key_and_record_are_exported_from_the_sub_select():
    rewritten, repairs = rewrite(rewrite_hidden_sub_select_variables, FOLLOW_UPS)
    assert repairs == [
        "Added ?follow_up and ?group_name to the sub-select's SELECT and GROUP BY: the outer query reads them, "
        "and a sub-select only exports what it projects."
    ]
    text = " ".join(body(rewritten))
    assert "SELECT ?g ?follow_up ?group_name (COUNT(DISTINCT ?follow_up) AS ?fu_count)" in text
    assert "GROUP BY ?g ?follow_up ?group_name }" in text  # the alias ?fu_count, its own group key, is dropped
    assert check_unbound_variables(parse_sparql(add_missing_prefixes(PREFIXES + rewritten))) == []


def test_hidden_subgroup_flag_read_by_an_outer_aggregate_is_exported():
    # q-060: the OPTIONAL flag is bound inside the sub-select, the outer COUNT reads it.
    query = ("SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?g_with_abx) AS ?with_abx) WHERE { { SELECT ?g "
             "(COUNT(DISTINCT ?lab) AS ?n) WHERE { GRAPH ?g { ?lab a sphn:MicroorganismIdentificationLabTestEvent . "
             "OPTIONAL { GRAPH ?g { ?abx a sphn:AntimicrobialSusceptibilityLabTestEvent . BIND(?g AS ?g_with_abx) } } } } "
             "GROUP BY ?g } }")
    rewritten, repairs = rewrite(rewrite_hidden_sub_select_variables, query)
    assert [repair.split(":")[0] for repair in repairs] == ["Added ?g_with_abx to the sub-select's SELECT and GROUP BY"]
    text = " ".join(body(rewritten))
    assert "SELECT ?g ?g_with_abx (COUNT(DISTINCT ?lab) AS ?n)" in text and "GROUP BY ?g ?g_with_abx }" in text


def test_sub_select_without_group_by_gets_one_and_distinct_stays_plain():
    aggregated = ("SELECT ?year ?n WHERE { { SELECT (COUNT(?x) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Death ; "
                  "sphn:hasDate ?d BIND(YEAR(?d) AS ?year) } } } } ORDER BY ?year")
    rewritten, repairs = rewrite(rewrite_hidden_sub_select_variables, aggregated)
    assert len(repairs) == 1
    assert "SELECT ?year (COUNT(?x) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Death ; sphn:hasDate ?d BIND(YEAR(?d) AS ?year) } } GROUP BY ?year }" in " ".join(body(rewritten))
    distinct = ("SELECT ?year (COUNT(DISTINCT ?g) AS ?patients) WHERE { { SELECT DISTINCT ?g WHERE { GRAPH ?g { ?x a sphn:Death ; "
                "sphn:hasDate ?d } BIND(YEAR(?d) AS ?year) } } } GROUP BY ?year")
    rewritten, repairs = rewrite(rewrite_hidden_sub_select_variables, distinct)
    assert len(repairs) == 1
    assert "SELECT DISTINCT ?g ?year WHERE" in " ".join(body(rewritten)) and "GROUP BY ?year" in rewritten


def test_the_scope_hint_stays_when_the_outer_query_reads_the_sub_select_alias():
    # q-032: the outer AVG reads ?encounters, which a new group key would change, so nothing is exported.
    query = ("SELECT ?specialty (AVG(?encounters) AS ?avg) WHERE { { SELECT ?g (COUNT(DISTINCT ?e) AS ?encounters) WHERE { "
             "GRAPH ?g { ?e a sphn:HealthcareEncounter ; sphn:hasTherapeuticArea ?a . ?a sphn:hasSpecialtyName ?specialty } } "
             "GROUP BY ?g } } GROUP BY ?specialty")
    fixed, parsed = prepare(query)
    assert rewrite_hidden_sub_select_variables(fixed, parsed) == (fixed, [])
    assert check_unbound_variables(parsed) == [
        "?specialty is used in SELECT/GROUP BY but is not bound in this scope (a sub-select only exports the "
        "variables it projects): add ?specialty to the sub-select's SELECT."
    ]


def test_ambiguous_or_graph_only_sub_selects_are_left_alone():
    twins = ("SELECT ?year (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g WHERE { GRAPH ?g { ?x a sphn:Death ; sphn:hasDate ?d } "
             "BIND(YEAR(?d) AS ?year) } } { SELECT ?g WHERE { GRAPH ?g { ?b a sphn:Birth } } } } GROUP BY ?year")
    fixed, parsed = prepare(twins)
    assert rewrite_hidden_sub_select_variables(fixed, parsed) == (fixed, [])
    graph_only = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT (COUNT(?x) AS ?deaths) WHERE { GRAPH ?g { ?x a sphn:Death } } } }"
    fixed, parsed = prepare(graph_only)
    assert rewrite_hidden_sub_select_variables(fixed, parsed) == (fixed, [])


# ---------------------------------------------------------------- COUNT of a misnamed record (R7)

PRESCRIPTIONS = (  # q-068: the record is ?drug_prescription, the COUNT reads ?prescription
    "SELECT ?cohort (COUNT(DISTINCT ?g) AS ?patients) (AVG(?prescriptions) AS ?avg) WHERE { { SELECT ?g "
    "(COUNT(DISTINCT ?prescription) AS ?prescriptions) WHERE { GRAPH ?g { ?drug_prescription a sphn:DrugPrescription } "
    "OPTIONAL { GRAPH ?g { ?drug_prescription sphn:hasAdministrativeCase ?case } } } GROUP BY ?g } "
    "BIND(IF(EXISTS { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }, \"with\", \"without\") AS ?cohort) } GROUP BY ?cohort"
)


def test_count_of_a_misnamed_record_is_renamed_to_the_typed_record():
    rewritten, repairs = rewrite(rewrite_count_of_misnamed_record, PRESCRIPTIONS)
    assert repairs == [
        "Renamed ?prescription to ?drug_prescription in COUNT(?prescription): nothing binds ?prescription, and "
        "?drug_prescription is the sphn:DrugPrescription record in scope."
    ]
    assert "(COUNT(DISTINCT ?drug_prescription) AS ?prescriptions)" in rewritten and "?prescription)" not in rewritten
    assert check_unbound_variables(parse_sparql(add_missing_prefixes(PREFIXES + rewritten))) == []


@pytest.mark.parametrize("query", [
    PRESCRIPTIONS.replace("?drug_prescription a sphn:DrugPrescription", "?drug_prescription a sphn:DrugPrescription . ?old_prescription a sphn:DrugPrescription"),
    PRESCRIPTIONS.replace("\"with\", \"without\")", "\"with\", ?prescription)"),  # read elsewhere: unbound on purpose?
    ("SELECT (COUNT(DISTINCT ?prescription) AS ?n) WHERE { { SELECT ?g WHERE { GRAPH ?g { ?drug_prescription a sphn:DrugPrescription } } } }"),
], ids=["two candidates", "also read by IF", "record typed only inside a sub-select"])
def test_rename_needs_one_record_in_scope_and_a_variable_used_only_in_count(query):
    fixed, parsed = prepare(query)
    assert rewrite_count_of_misnamed_record(fixed, parsed) == (fixed, [])


# ---------------------------------------------------------------- record link typed with a code (R9)

INTENTS = (  # q-066: the Intent record typed with the SNOMED code of its hasCode node, in two UNION branches
    "SELECT ?intent_concept (COUNT(DISTINCT ?proc) AS ?procedures) WHERE { GRAPH ?g { "
    "{ ?proc a sphn:BilledProcedure ; sphn:hasIntent ?intent . ?intent a snomed:373808002 . } UNION "
    f"{{ ?proc a sphn:ImagingProcedure ; sphn:hasIntent ?intent . ?intent a snomed:363676003 {OPTION} . }} }} }} "
    "GROUP BY ?intent_concept"
)


def test_record_link_object_typed_with_a_code_hops_through_has_code(schema):
    rewritten, repairs = rewrite(rewrite_record_link_typed_with_code, INTENTS, schema=schema)
    assert repairs == [
        "Wrote ?intent sphn:hasCode ?intent_code . ?intent_code a snomed:373808002 instead of '?intent a snomed:373808002': "
        "?intent is a sphn:Intent record (object of sphn:hasIntent), and its code sits on sphn:hasCode.",
        "Wrote ?intent sphn:hasCode ?intent_code . ?intent_code a snomed:363676003 instead of '?intent a snomed:363676003': "
        "?intent is a sphn:Intent record (object of sphn:hasIntent), and its code sits on sphn:hasCode.",
    ]
    text = " ".join(body(rewritten))
    assert "?intent sphn:hasCode ?intent_code . ?intent_code a snomed:373808002 . }" in text
    assert f"?intent sphn:hasCode ?intent_code . ?intent_code a snomed:363676003 {OPTION} . }}" in text  # the option moves with the type
    assert rewrite(rewrite_record_link_typed_with_code, INTENTS) == (rewritten, repairs)  # the cards load by themselves


@pytest.mark.parametrize("statement,expected", [
    ("?intent a snomed:373808002 ; rdfs:label ?l .", "?intent_code a snomed:373808002 . ?intent sphn:hasCode ?intent_code ; rdfs:label ?l ."),
    ("?intent rdfs:label ?l ; a snomed:373808002 .", "?intent rdfs:label ?l ; sphn:hasCode ?intent_code . ?intent_code a snomed:373808002 ."),
    ("?intent a snomed:373808002", "?intent sphn:hasCode ?intent_code . ?intent_code a snomed:373808002"),
], ids=["head of a chain", "tail of a chain", "last statement without a dot"])
def test_hop_handles_chains_and_the_last_statement(schema, statement, expected):
    query = f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?proc a sphn:BilledProcedure ; sphn:hasIntent ?intent . {statement} }} }}"
    rewritten, repairs = rewrite(rewrite_record_link_typed_with_code, query, schema=schema)
    assert len(repairs) == 1 and expected in " ".join(body(rewritten))


@pytest.mark.parametrize("pattern", [
    "?proc sphn:hasIntent ?intent . ?intent a ?concept",  # a variable class: the schema check's hint
    "?proc sphn:hasIntent ?intent . ?intent a sphn:Intent",  # its own class
    "?proc sphn:hasCode ?code . ?code a snomed:373808002",  # a code property: the code node is typed, as it should be
    "?proc sphn:hasResult ?r . ?r a snomed:373808002",  # several target classes
    "?proc sphn:hasQuantity ?q . ?q a snomed:373808002",  # a target without hasCode
], ids=["variable class", "own class", "code property", "several targets", "target without hasCode"])
def test_hop_leaves_other_typings_alone(schema, pattern):
    fixed, parsed = prepare(f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?proc a sphn:BilledProcedure . {pattern} }} }}")
    assert rewrite_record_link_typed_with_code(fixed, parsed, schema) == (fixed, [])
    assert rewrite_record_link_typed_with_code(fixed, parsed, SchemaCards({}, [])) == (fixed, [])


# ---------------------------------------------------------------- VALUES on a code node (R-vcode)

CARE_TYPES = (  # q-022: the VALUES binds the code node of hasTypeCode to concept IRIs
    "SELECT ?care_type (COUNT(DISTINCT ?g) AS ?stays) WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase ; "
    "sphn:hasCareHandling ?ch . ?ch sphn:hasTypeCode ?code . VALUES (?code ?care_type) { (snomed:371883000 \"Outpatient\") "
    "(snomed:394656005 \"Inpatient\") } } } GROUP BY ?care_type"
)


def test_values_on_a_code_node_types_it_and_binds_the_concept():
    rewritten, repairs = rewrite(rewrite_values_on_code_node, CARE_TYPES)
    assert repairs == [
        "VALUES bound ?code, the code node of sphn:hasTypeCode, to concept IRIs, which a code node never equals: "
        "added ?code a ?code_concept and moved the VALUES to ?code_concept."
    ]
    text = " ".join(body(rewritten))
    assert "?ch sphn:hasTypeCode ?code . ?code a ?code_concept . VALUES (?code_concept ?care_type) {" in text
    icd = ("SELECT ?label (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code } "
           "VALUES (?code ?label) { (icd-10-gm:A00.0 \"classical\") (icd-10-gm:A00.1 \"el tor\") } } GROUP BY ?label")
    rewritten, repairs = rewrite(rewrite_values_on_code_node, icd)
    assert repairs[0].endswith("added ?code a ?code_concept with the inference option and moved the VALUES to ?code_concept.")
    assert f"?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?code_concept {OPTION} }} VALUES (?code_concept ?label)" in " ".join(body(rewritten))


def test_values_rewrite_follows_the_other_reads_and_a_statement_without_a_dot():
    query = ("SELECT ?code (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?ch a sphn:CareHandling ; sphn:hasTypeCode ?code } "
             "VALUES ?code { snomed:371883000 snomed:394656005 } FILTER(?code != snomed:394656005) } GROUP BY ?code")
    rewritten, repairs = rewrite(rewrite_values_on_code_node, query)
    assert len(repairs) == 1
    assert " ".join(body(rewritten)) == (
        "SELECT ?code_concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?ch a sphn:CareHandling ; sphn:hasTypeCode ?code . "
        "?code a ?code_concept } VALUES ?code_concept { snomed:371883000 snomed:394656005 } FILTER(?code_concept != snomed:394656005) } "
        "GROUP BY ?code_concept"
    )


@pytest.mark.parametrize("query", [
    "SELECT ?label (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?ch sphn:hasTypeCode ?code . ?code a ?concept } VALUES (?concept ?label) { (snomed:371883000 \"out\") } } GROUP BY ?label",
    "SELECT ?label (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?ch sphn:hasTypeCode ?code . ?code a snomed:371883000 } VALUES (?code ?label) { (snomed:371883000 \"out\") } } GROUP BY ?label",
    "SELECT ?label (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?ch sphn:hasTypeCode ?code } VALUES (?code ?label) { (\"x\" \"out\") } } GROUP BY ?label",
], ids=["VALUES on the concept", "typed code node", "VALUES of strings"])
def test_values_that_bind_a_concept_or_a_typed_node_are_left_alone(query):
    fixed, parsed = prepare(query)
    assert rewrite_values_on_code_node(fixed, parsed) == (fixed, [])


# ---------------------------------------------------------------- path inside a FILTER (R8)

IMPLANTS = (  # q-069: the pseudo-identifier "path" inside FILTER is a division
    f"SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?implant) AS ?episodes) WHERE {{ {SEX} "
    "OPTIONAL { GRAPH ?g { ?implant a sphn:ImplantPresence ; sphn:hasSubjectPseudoIdentifier ?pid ; sphn:hasStartDateTime ?start . "
    "FILTER(?pid = ?sex_record/sphn:hasSubjectPseudoIdentifier) } } } GROUP BY ?sex"
)


def test_pseudo_identifier_path_filter_is_dropped_with_its_triple():
    rewritten, repairs = rewrite(rewrite_path_in_filter, IMPLANTS)
    assert repairs == [
        "Removed FILTER(?pid = ?sex_record/sphn:hasSubjectPseudoIdentifier) and the ?pid triple: '/' is a division inside "
        "an expression, not a path, and ?implant and ?sex_record are already in the same GRAPH ?g (one patient)."
    ]
    text = " ".join(body(rewritten))
    assert "?pid" not in text and "FILTER" not in text
    assert "?implant a sphn:ImplantPresence ; sphn:hasStartDateTime ?start . } }" in text
    assert hints(check_path_in_expression, rewritten) == []
    mirrored = IMPLANTS.replace("?pid = ?sex_record/sphn:hasSubjectPseudoIdentifier", "?sex_record/sphn:hasSubjectPseudoIdentifier = ?pid")
    assert rewrite(rewrite_path_in_filter, mirrored) == (rewritten, repairs)


def test_path_filter_stays_and_gets_the_hint_when_it_cannot_go():
    other_graph = IMPLANTS.replace("OPTIONAL { GRAPH ?g {", "OPTIONAL { GRAPH ?h {")
    fixed, parsed = prepare(other_graph)
    assert rewrite_path_in_filter(fixed, parsed) == (fixed, [])
    assert check_path_in_expression(parsed, fixed) == [
        "?sex_record / sphn:hasSubjectPseudoIdentifier inside an expression is a division, not a property path: bind the "
        "value with a triple pattern, ?sex_record sphn:hasSubjectPseudoIdentifier ?sex_record_subject_pseudo_identifier, and "
        "compare ?sex_record_subject_pseudo_identifier. Records of the same patient are already in the same GRAPH ?g: "
        "match the second record there."
    ]
    read_elsewhere = IMPLANTS.replace("GROUP BY ?sex", "BIND(STR(?pid) AS ?s) } GROUP BY ?sex").replace("} } } GROUP BY", "} } GROUP BY", 1)
    fixed, parsed = prepare(read_elsewhere.replace("} } BIND", "} BIND"))
    assert rewrite_path_in_filter(fixed, parsed) == (fixed, [])
    date_path = ("SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?c a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?d . "
                 "?r a sphn:Death FILTER(?d < ?r/sphn:hasDate) } }")
    assert hints(check_path_in_expression, date_path) == [
        "?r / sphn:hasDate inside an expression is a division, not a property path: bind the value with a triple pattern, "
        "?r sphn:hasDate ?r_date, and compare ?r_date."
    ]


# ---------------------------------------------------------------- BIND out of scope (C-scope)

PATHWAYS = (  # official-01: the datediff BIND inside OPTIONAL cannot see the diagnosis date
    "SELECT ?treatment (COUNT(DISTINCT ?g) AS ?patients) (ROUND(AVG(?wait_surg)) AS ?wait) WHERE { GRAPH ?g { "
    "?diag a sphn:OncologyDiagnosis ; sphn:hasRecordDateTime ?diag_date . OPTIONAL { GRAPH ?g { ?surg a sphn:OncologySurgery ; "
    "sphn:hasStartDateTime ?surg_start } BIND(bif:datediff(\"day\", ?diag_date, ?surg_start) AS ?wait_surg) "
    "BIND(IF(BOUND(?surg_start), ?g, ?unbound) AS ?surgery_patient) } } "
    "BIND(IF(BOUND(?surgery_patient), \"surgery\", \"nothing\") AS ?treatment) } GROUP BY ?treatment"
)


def test_bind_inside_optional_that_reads_an_outer_variable_gets_the_guarded_form():
    assert hints(check_bind_out_of_scope, PATHWAYS) == [
        "BIND(... AS ?wait_surg) inside OPTIONAL cannot see ?diag_date (bound outside it): bif:datediff fails (DT001). "
        "Compute it after the OPTIONAL: BIND(IF(BOUND(?surg_start), bif:datediff(\"day\", ?diag_date, ?surg_start), ?none) AS ?wait_surg)"
    ]
    # q-064: an OPTIONAL that holds only a BIND on a sub-select alias; YEAR() names the failing function.
    flag = ("SELECT (SUM(?many) AS ?n) WHERE { { SELECT ?g (COUNT(?p) AS ?prescriptions) WHERE { GRAPH ?g { ?p a sphn:DrugPrescription } } "
            "GROUP BY ?g } OPTIONAL { BIND(IF(?prescriptions > 3, 1, 0) AS ?many) } }")
    assert hints(check_bind_out_of_scope, flag) == [
        "BIND(... AS ?many) inside OPTIONAL cannot see ?prescriptions (bound outside it): ?many stays empty. "
        "Compute it after the OPTIONAL: BIND(IF(?prescriptions > 3, 1, 0) AS ?many)"
    ]
    year = ("SELECT ?year (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?c a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?d . "
            "OPTIONAL { ?x a sphn:Death BIND(YEAR(?d) AS ?year) } } } GROUP BY ?year")
    assert hints(check_bind_out_of_scope, year)[0].startswith("BIND(... AS ?year) inside OPTIONAL cannot see ?d (bound outside it): YEAR() fails (DT001).")


BIRTHS = ("GRAPH ?g { ?birth a sphn:Birth ; sphn:hasDeliveryModeCode ?code ; sphn:hasGestationalAge ?age . ?code a ?concept . "
          "?age sphn:hasQuantity ?q . ?q sphn:hasValue ?value } ")


def test_the_suggested_bind_keeps_the_filter_of_its_optional_block():
    # official-10: the FILTER is the condition of the count; the bare BIND(?g AS ?early_patient) counted every birth
    query = (f"SELECT ?concept (COUNT(DISTINCT ?early_patient) AS ?early) WHERE {{ {BIRTHS} "
             "OPTIONAL { FILTER (?value < 37) BIND (?g AS ?early_patient) } } GROUP BY ?concept")
    assert hints(check_bind_out_of_scope, query) == [
        "BIND(... AS ?early_patient) inside OPTIONAL cannot see ?g (bound outside it): ?early_patient stays empty. "
        "Compute it after the OPTIONAL: BIND(IF(?value < 37, STR(?g), ?none) AS ?early_patient)"
    ]
    # official-07's first try: a record, inside GRAPH ?g, under two conditions
    query = ("SELECT (COUNT(DISTINCT ?in_stay) AS ?n) WHERE { GRAPH ?g { ?admin a sphn:DrugAdministrationEvent ; "
             "sphn:hasStartDateTime ?start ; sphn:hasAdministrativeCase ?case . ?case sphn:hasAdmissionDateTime ?admission ; "
             "sphn:hasDischargeDateTime ?discharge . OPTIONAL { FILTER (?start >= ?admission && ?start <= ?discharge) "
             "BIND (?admin AS ?in_stay) } } }")
    assert hints(check_bind_out_of_scope, query) == [
        "BIND(... AS ?in_stay) inside OPTIONAL cannot see ?admin (bound outside it): ?in_stay stays empty. Compute it "
        "after the OPTIONAL: BIND(IF(?start >= ?admission && ?start <= ?discharge, ?admin, ?none) AS ?in_stay)"
    ]


def test_the_filter_goes_before_the_bound_guards_and_a_filter_on_the_bind_itself_is_left_out():
    alternatives = (f"SELECT (AVG(?gap) AS ?avg) WHERE {{ {BIRTHS} OPTIONAL {{ GRAPH ?g {{ ?death a sphn:Death ; "
                    "sphn:hasDate ?died } FILTER (?value IN (1, 2) || ?value > 300) BIND (?value - ?died AS ?gap) } }")
    assert hints(check_bind_out_of_scope, alternatives) == [
        "BIND(... AS ?gap) inside OPTIONAL cannot see ?value (bound outside it): ?gap stays empty. Compute it after the "
        "OPTIONAL: BIND(IF((?value IN (1, 2) || ?value > 300) && BOUND(?died), ?value - ?died, ?none) AS ?gap)"
    ]
    own = (f"SELECT (COUNT(DISTINCT ?early_patient) AS ?early) WHERE {{ {BIRTHS} "
           "OPTIONAL { BIND (IF(?value < 37, ?g, ?unbound) AS ?early_patient) FILTER (BOUND(?early_patient)) } }")
    assert hints(check_bind_out_of_scope, own) == [
        "BIND(... AS ?early_patient) inside OPTIONAL cannot see ?value (bound outside it): ?early_patient stays empty. "
        "Compute it after the OPTIONAL: BIND(IF(?value < 37, ?g, ?unbound) AS ?early_patient)"
    ]


def test_bind_after_a_sub_select_that_hides_its_variables_is_reported():
    # q-032: the sub-select binds ?start and ?end but projects neither.
    query = ("SELECT ?specialty (AVG(?hours) AS ?avg) WHERE { { SELECT ?g ?specialty (COUNT(DISTINCT ?e) AS ?encounters) WHERE { GRAPH ?g { "
             "?e a sphn:HealthcareEncounter ; sphn:hasStartDateTime ?start ; sphn:hasEndDateTime ?end ; sphn:hasTherapeuticArea ?a . "
             "?a sphn:hasSpecialtyName ?specialty } } GROUP BY ?g ?specialty } BIND(bif:datediff(\"hour\", ?start, ?end) AS ?hours) } GROUP BY ?specialty")
    assert hints(check_bind_out_of_scope, query) == [
        "BIND(... AS ?hours) cannot see ?end and ?start: the sub-select binds them but exports only what it projects, so "
        "bif:datediff fails (DT001). Add ?end ?start to the sub-select's SELECT and GROUP BY, or compute ?hours inside it."
    ]


def test_bind_before_the_pattern_that_binds_its_input_is_reported():
    query = ("SELECT ?year (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { BIND(YEAR(?d) AS ?year) ?c a sphn:AdministrativeCase ; "
             "sphn:hasAdmissionDateTime ?d } } GROUP BY ?year")
    assert hints(check_bind_out_of_scope, query) == [
        "BIND(... AS ?year) cannot see ?d: it is bound in a later pattern or in another block (an OPTIONAL or a UNION branch), "
        "and a BIND sees only the patterns before it in its own block, so YEAR() fails (DT001). Move the BIND after the "
        "pattern that binds ?d."
    ]


@pytest.mark.parametrize("query", [
    # q-060: BIND(?g AS ?x) inside GRAPH ?g inside OPTIONAL sees the graph variable
    "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { GRAPH ?g { ?l a sphn:LabTest OPTIONAL { GRAPH ?g { ?a a sphn:Death BIND(?g AS ?x) } } } }",
    # the conditional count of the references: the BIND stands after the GRAPH block of its OPTIONAL
    "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { GRAPH ?g { ?l a sphn:LabTest } OPTIONAL { GRAPH ?g { ?a a sphn:Death } BIND(?g AS ?x) } }",
    # IF() arms, COALESCE() and BOUND() may read unbound variables on purpose (official-04's reference)
    "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { GRAPH ?g { ?b a sphn:Birth } OPTIONAL { GRAPH ?g { ?d a sphn:Death ; sphn:hasDate ?dd } } "
    "BIND(IF(BOUND(?dd), ?g, ?never) AS ?x) BIND(COALESCE(?dd, ?never) AS ?y) }",
    # a variable bound in an earlier OPTIONAL is in scope for a later BIND
    "SELECT (AVG(?days) AS ?avg) WHERE { GRAPH ?g { ?c a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?a } "
    "OPTIONAL { GRAPH ?g { ?c sphn:hasDischargeDateTime ?d } } BIND(bif:datediff(\"day\", ?a, ?d) AS ?days) }",
    # a FILTER inside OPTIONAL sees the outer variables (official-01's reference); SELECT expressions read aggregates
    "SELECT (COUNT(DISTINCT ?g) AS ?n) (ROUND(100 * COUNT(DISTINCT ?s) / COUNT(DISTINCT ?g)) AS ?pct) WHERE { GRAPH ?g { ?dx a sphn:OncologyDiagnosis ; "
    "sphn:hasIncidenceDateTime ?i } OPTIONAL { GRAPH ?g { ?s a sphn:OncologySurgery ; sphn:hasStartDateTime ?ss } FILTER(?ss > ?i) } }",
], ids=["BIND(?g) inside GRAPH ?g", "BIND after the GRAPH block", "IF arms, COALESCE, BOUND", "earlier OPTIONAL", "FILTER in OPTIONAL and SELECT expressions"])
def test_binds_that_see_their_variables_are_fine(query):
    assert hints(check_bind_out_of_scope, query) == []


def test_out_of_scope_hints_carry_the_hard_error_marker():
    for query in (PATHWAYS,):
        for hint in hints(check_bind_out_of_scope, query):
            assert hint.startswith("BIND(") and "cannot see" in hint


# ---------------------------------------------------------------- nested comparison (C-nest)

RADIOTHERAPY = (  # q-020: a date compared with the truth value of a datediff comparison
    "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?surgery a sphn:OncologySurgery ; sphn:hasStartDateTime ?surgery_start . "
    "OPTIONAL { ?rt a sphn:RadiotherapyProcedure ; sphn:hasStartDateTime ?rt_start } "
    "FILTER (BOUND(?rt_start) && ?rt_start > ?surgery_start && ?rt_start <= (bif:datediff(\"day\", ?surgery_start, ?rt_start) <= 90)) } }"
)


def test_date_compared_with_a_datediff_comparison_keeps_the_inner_comparison():
    rewritten, repairs = rewrite(rewrite_nested_comparison, RADIOTHERAPY)
    assert repairs == [
        "Replaced ?rt_start <= (<comparison>) by the inner comparison: a comparison yields true or false, which cannot "
        "be compared with the date ?rt_start."
    ]
    assert "?rt_start > ?surgery_start && (bif:datediff(\"day\", ?surgery_start, ?rt_start) <= 90))" in rewritten
    assert hints(check_nested_comparison, rewritten) == []
    mirrored = RADIOTHERAPY.replace("?rt_start <= (bif:datediff(\"day\", ?surgery_start, ?rt_start) <= 90)",
                                    "(bif:datediff(\"day\", ?surgery_start, ?rt_start) <= 90) >= ?rt_start")
    assert rewrite(rewrite_nested_comparison, mirrored)[0] == rewritten


def test_other_nested_comparisons_get_the_hint():
    assert hints(check_nested_comparison, RADIOTHERAPY) == [
        "FILTER: ?rt_start <= bif:datediff(\"day\", ?surgery_start, ?rt_start) <= 90 compares a date with true/false, because "
        "(bif:datediff(\"day\", ?surgery_start, ?rt_start) <= 90) is itself a comparison, and Virtuoso rejects it (SP031). "
        "Write each condition on its own, joined with &&: FILTER(?a > ?b && bif:datediff(\"day\", ?b, ?a) <= 90)."
    ]
    value = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasValue ?v } FILTER(?v <= (?v <= 90)) }"
    fixed, parsed = prepare(value)
    assert rewrite_nested_comparison(fixed, parsed) == (fixed, [])
    assert check_nested_comparison(parsed, fixed)[0].startswith("FILTER: ?v <= ?v <= 90 compares a value with true/false")


# ---------------------------------------------------------------- COUNT alias named after a record (H1)

GUIDED = (  # q-074's first try: ?guided_proc is never bound and ?proc is the typed record
    "SELECT ?year (COUNT(DISTINCT ?proc) AS ?procedures) (COUNT(DISTINCT ?guided_proc) AS ?guided) WHERE { "
    "{ SELECT ?g ?year ?proc WHERE { GRAPH ?g { ?proc a sphn:ImagingProcedure ; sphn:hasStartDateTime ?start BIND(YEAR(?start) AS ?year) } } } "
    "OPTIONAL { GRAPH ?g { ?proc sphn:hasIntent ?intent } } } GROUP BY ?year"
)
RECORD_HINT = (
    "COUNT(?guided_proc): nothing binds ?guided_proc. It counts ?proc records, so bind the record: "
    "OPTIONAL { GRAPH ?g { <condition on ?proc> } BIND(?proc AS ?guided_proc) } and COUNT(DISTINCT ?guided_proc)."
)


def test_count_alias_named_after_a_typed_record_binds_the_record():
    fixed, parsed = prepare(GUIDED)
    project = _find(parsed.algebra, "Project")[0]
    assert record_subgroup_hint(project, Variable("guided_proc")) == RECORD_HINT
    assert check_unbound_count_records(parsed, fixed) == [RECORD_HINT]
    assert len(check_unbound_variables(parsed)) == 1  # the same COUNT, as scope.py's own unbound check reports it


@pytest.mark.parametrize("query", [
    "SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?cholera_prescription) AS ?cholera) WHERE { GRAPH ?g { ?p a sphn:DrugPrescription } }",
    "SELECT (COUNT(DISTINCT ?both_patient) AS ?both) WHERE { GRAPH ?g { ?surgery a sphn:OncologySurgery } }",
    "SELECT (COUNT(DISTINCT ?z_a_b) AS ?n) WHERE { GRAPH ?g { ?b a sphn:Death . ?a_b a sphn:Birth } }",
    "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { GRAPH ?g { ?p a sphn:DrugPrescription } OPTIONAL { GRAPH ?g { ?d a sphn:Death } BIND(?g AS ?x) } }",
], ids=["no typed record of that name", "patient-named alias", "two candidates", "bound alias"])
def test_other_count_aliases_keep_the_patient_form(query):
    fixed, parsed = prepare(query)
    assert check_unbound_count_records(parsed, fixed) == []
    for project in _find(parsed.algebra, "Project"):
        for variable in project.PV:
            assert record_subgroup_hint(project, variable) is None


# ---------------------------------------------------------------- lists, other query forms, corpora

def test_module_lists_every_rewrite_and_check():
    assert REWRITES == [
        rewrite_hidden_sub_select_variables, rewrite_count_of_misnamed_record, rewrite_record_link_typed_with_code,
        rewrite_values_on_code_node, rewrite_path_in_filter, rewrite_nested_comparison,
    ]
    assert CHECKS == [check_bind_out_of_scope, check_nested_comparison, check_path_in_expression]


@pytest.mark.parametrize("query", ["ASK { ?s ?p ?o }", "SELECT ?x WHERE { ?x "])
def test_other_query_forms_and_invalid_text_are_left_alone(query):
    parsed = parse_sparql(query)
    for function in REWRITES:
        assert function(query, parsed) == (query, [])
    for check in CHECKS + [check_unbound_count_records]:
        assert check(parsed, query) == []


@pytest.mark.parametrize("example", load_examples(), ids=lambda example: example["id"])
def test_no_rewrite_or_check_touches_the_official_examples(example):
    text = add_missing_prefixes(example["sparql"])
    parsed = parse_sparql(text)
    for function in REWRITES:
        assert function(text, parsed) == (text, []), example["id"]
    assert [check(parsed, text) for check in CHECKS + [check_unbound_count_records]] == [[] for _ in range(len(CHECKS) + 1)]


@pytest.mark.parametrize("case_id,query", load_dev_references(), ids=[case[0] for case in load_dev_references()])
def test_no_rewrite_or_check_touches_the_team_examples_and_dev_references(case_id, query):
    text = add_missing_prefixes(query)
    parsed = parse_sparql(text)
    assert parsed.kind == "SelectQuery"
    for function in REWRITES:
        assert function(text, parsed) == (text, []), case_id
    assert [check(parsed, text) for check in CHECKS + [check_unbound_count_records]] == [[] for _ in range(len(CHECKS) + 1)]
