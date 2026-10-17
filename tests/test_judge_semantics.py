import json

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.judge_checks import semantics
from nl2sparql.judge_checks.semantics import (
    CHECKS,
    REWRITES,
    check_code_node_compared_with_concept,
    check_date_function_on_record,
    check_duplicate_aggregate_columns,
    check_fake_snomed_iri,
    check_graph_count_named_as_records,
    check_pseudo_identifier_join,
    check_unconditional_subgroup,
    check_unused_pseudo_identifier,
    check_values_codes_without_inference,
    rewrite_date_plus_duration,
    rewrite_inference_on_variable_class,
)
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes

ROOT = "https://biomedit.ch/rdf/sphn-resource/global"
OPTION = f'OPTION (inference "{ROOT}")'


def hints(check, query: str) -> list[str]:
    """Run one check the way the judge does: prefixes added, parsed, text and algebra passed along."""
    fixed = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(fixed)
    assert parsed.kind == "SelectQuery", parsed.error
    return check(parsed, fixed)


def dev_reference_queries() -> list[tuple[str, str]]:
    """The official examples and the dev reference queries; holdout rows are a blind exam and stay closed."""
    queries = [(row["id"], row["sparql"]) for row in load_examples()]
    lines = (DATA_DIR.parent / "eval" / "query_questions.jsonl").read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.strip():
            row = json.loads(line)
            if row.get("split") == "dev":
                queries.append((row["id"], row["reference_sparql"]))
    return queries


def test_check_and_rewrite_lists_expose_every_function():
    assert CHECKS == [
        check_date_function_on_record, check_graph_count_named_as_records, check_duplicate_aggregate_columns,
        check_unconditional_subgroup, check_pseudo_identifier_join, check_unused_pseudo_identifier,
        check_code_node_compared_with_concept, check_values_codes_without_inference, check_fake_snomed_iri,
    ]
    assert REWRITES == [rewrite_inference_on_variable_class, rewrite_date_plus_duration]


@pytest.mark.parametrize("case_id,query", dev_reference_queries(), ids=[case[0] for case in dev_reference_queries()])
def test_references_pass_every_check_and_no_rewrite_touches_them(case_id, query):
    for rewrite in REWRITES:
        assert rewrite(query) == (query, []), case_id
    fixed = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(fixed)
    if parsed.kind != "SelectQuery":  # official-14 is a CONSTRUCT, which the guard rejects
        return
    for check in CHECKS:
        assert check(parsed, fixed) == [], (case_id, check.__name__)


# ------------------------------------------------------------------------------------------ rewrites
def test_rewrite_strips_inference_from_a_variable_class():
    query = (
        "SELECT ?severity_class (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?event a sphn:AdverseEvent ; sphn:hasSeverityCode ?severity_code . "
        f"?severity_code a ?severity_class {OPTION} . "
        f"?event sphn:hasOutcome ?outcome . ?outcome rdf:type ?kind {OPTION.replace(chr(34), chr(39))} . "
        "} } GROUP BY ?severity_class"
    )
    rewritten, repairs = rewrite_inference_on_variable_class(query)
    assert repairs == ["inference_on_variable_class"]
    assert "OPTION" not in rewritten
    assert "?severity_code a ?severity_class ." in rewritten and "?outcome rdf:type ?kind ." in rewritten
    assert parse_sparql(add_missing_prefixes(rewritten)).kind == "SelectQuery"


@pytest.mark.parametrize("pattern", [
    f"?c a icd-10-gm:A00 {OPTION} .",
    f'VALUES (?concept ?label) {{ (icd-10-gm:A00 "cholera") }} ?c a ?concept {OPTION} .',
    f"VALUES ?concept {{ icd-10-gm:A00 }} ?c a ?concept {OPTION} .",
    f"?c a ?concept {OPTION} . FILTER (?concept = icd-10-gm:A00)",
    f"?c a ?concept {OPTION} . FILTER (?concept IN (icd-10-gm:A00, icd-10-gm:A01))",
    f"?c a ?concept {OPTION} . FILTER (icd-10-gm:A00 != ?concept)",
], ids=["concrete-code", "values-pair", "values-single", "equals", "in", "right-hand-side"])
def test_rewrite_keeps_inference_on_concrete_codes_and_pinned_class_variables(pattern):
    query = f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?x sphn:hasCode ?c . {pattern} }} }}"
    assert rewrite_inference_on_variable_class(query) == (query, [])


def test_rewrite_turns_date_plus_duration_into_dateadd():
    query = (
        "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
        "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?surgery a sphn:OncologySurgery ; sphn:hasStartDateTime ?surgery_start . "
        "?rt a sphn:RadiotherapyProcedure ; sphn:hasStartDateTime ?rt_start . "
        'FILTER (?rt_start > ?surgery_start - \'P7D\' && ?rt_start <= ?surgery_start + "P90D"^^xsd:duration) } }'
    )
    rewritten, repairs = rewrite_date_plus_duration(query)
    assert repairs == ["date_plus_duration"]
    assert 'bif:dateadd("day", -7, ?surgery_start)' in rewritten
    assert 'bif:dateadd("day", 90, ?surgery_start)' in rewritten
    assert rewritten.startswith("PREFIX bif: <bif:>\n")  # the rewrite introduces bif: and declares it
    assert parse_sparql(add_missing_prefixes(rewritten)).kind == "SelectQuery"


def test_rewrite_leaves_other_durations_and_plain_arithmetic_alone():
    query = 'SELECT ?x WHERE { FILTER (?d <= ?start + "P1M" && ?n > ?m + 1) }'
    assert rewrite_date_plus_duration(query) == (query, [])


# ------------------------------------------------------------------------ 1. date function on a record
def test_year_on_a_record_names_its_date_property():
    query = (
        "SELECT ?year (COUNT(DISTINCT ?g) AS ?deaths) WHERE { GRAPH ?g { ?death a sphn:Death . } "
        "BIND (YEAR(?death) AS ?year) } GROUP BY ?year"
    )
    assert hints(check_date_function_on_record, query) == [
        "?death is a sphn:Death record, not a date: apply YEAR() to one of its dateTime properties, "
        "e.g. ?death sphn:hasReportDateTime ?death_report and YEAR(?death_report)."
    ]


def test_year_on_a_date_parts_record_points_at_the_parts_and_the_linking_record():
    query = (
        "SELECT ?year (COUNT(DISTINCT ?g) AS ?deaths) WHERE { GRAPH ?g { "
        "?death a sphn:Death ; sphn:hasDate ?death_date . ?death_date a sphn:DeathDate ; sphn:hasYear ?year . } "
        "FILTER (YEAR(?death_date) >= 2015 && YEAR(?death_date) <= 2025) } GROUP BY ?year"
    )
    assert hints(check_date_function_on_record, query) == [
        "?death_date is a sphn:DeathDate record, not a date: it stores the date as literal parts (sphn:hasYear, "
        "sphn:hasMonth, sphn:hasDay); use ?death_date sphn:hasYear ?year and compare ?year directly, without "
        "YEAR(). Or use the dateTime of the record that links to it: ?death sphn:hasReportDateTime ?death_report "
        "and YEAR(?death_report)."
    ]


def test_day_on_a_datediff_result_is_a_number_not_a_date():
    query = (
        "SELECT (AVG(DAY(?duration)) AS ?avg_days) WHERE { GRAPH ?g { "
        "?implant a sphn:ImplantPresence ; sphn:hasStartDateTime ?start ; sphn:hasEndDateTime ?end . "
        "BIND (bif:datediff('day', ?start, ?end) AS ?duration) } }"
    )
    assert hints(check_date_function_on_record, query) == [
        "?duration is already a number of days (bif:datediff), not a date: use ?duration without DAY()."
    ]
    nested = query.replace("BIND (bif:datediff('day', ?start, ?end) AS ?duration)", "").replace(
        "DAY(?duration)", "DAY(bif:datediff('hour', ?start, ?end))"
    )
    assert hints(check_date_function_on_record, nested) == [
        "bif:datediff returns a number of hours, not a date: use it without DAY()."
    ]


def test_datediff_on_a_record_lists_its_date_properties():
    query = (
        "SELECT (AVG(?hours) AS ?avg_hours) WHERE { GRAPH ?g { "
        "?proc a sphn:ImagingProcedure ; sphn:hasStartDateTime ?start . "
        '?diag a sphn:Diagnosis . BIND (bif:datediff("hour", ?start, ?proc) AS ?hours) '
        'BIND (bif:datediff("day", ?diag, ?start) AS ?wait) } }'
    )
    assert hints(check_date_function_on_record, query) == [
        "bif:datediff needs two dateTimes, but ?proc is a sphn:ImagingProcedure record: use one of its dateTime "
        "properties (sphn:hasEndDateTime, sphn:hasStartDateTime) instead, e.g. ?proc sphn:hasEndDateTime ?proc_end.",
        "bif:datediff needs two dateTimes, but ?diag is a sphn:Diagnosis record: use one of its dateTime "
        "properties instead, e.g. ?diag sphn:hasRecordDateTime ?diag_record.",
    ]


def test_date_function_on_an_untyped_record_gets_the_generic_hint():
    query = "SELECT ?m WHERE { GRAPH ?g { ?rec sphn:hasCode ?c . } BIND (MONTH(?rec) AS ?m) }"
    assert hints(check_date_function_on_record, query) == [
        "?rec is a record, not a date: apply MONTH() to one of its dateTime properties, e.g. ?rec sphn:hasStartDateTime ?start."
    ]


def test_date_functions_on_date_values_are_fine():
    query = (
        "SELECT ?year (AVG(?days) AS ?avg) WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase ; "
        "sphn:hasAdmissionDateTime ?start ; sphn:hasDischargeDateTime ?end . } "
        'BIND (YEAR(?start) AS ?year) BIND (bif:datediff("day", ?start, ?end) AS ?days) '
        "FILTER (MONTH(?end) > 6) } GROUP BY ?year"
    )
    assert hints(check_date_function_on_record, query) == []


# ------------------------------------------------------------------ 2. COUNT(DISTINCT ?g) named as records
def test_graph_count_under_a_record_name_counts_patients():
    query = (
        "SELECT ?certainty (COUNT(DISTINCT ?g) AS ?total_episodes) (COUNT(DISTINCT ?g) AS ?patients) WHERE { "
        "GRAPH ?g { ?episode a sphn:AllergyEpisode ; sphn:hasCertaintyCode ?code . ?code a ?certainty . } } "
        "GROUP BY ?certainty"
    )
    assert hints(check_graph_count_named_as_records, query) == [
        "COUNT(DISTINCT ?g) counts patients, but the column is named ?total_episodes. Count records with "
        "COUNT(DISTINCT ?episode) (the record variable) and patients with COUNT(DISTINCT ?g)."
    ]


@pytest.mark.parametrize("columns", [
    "(COUNT(DISTINCT ?g) AS ?patients)",
    "(COUNT(DISTINCT ?g) AS ?patients_with_drugs)",
    "(COUNT(DISTINCT ?g) AS ?n) (COUNT(DISTINCT ?g) AS ?total)",
    "(COUNT(DISTINCT ?g) AS ?births)",
    "(COUNT(DISTINCT ?g) AS ?latest)",
    "(COUNT(DISTINCT ?episode) AS ?total_episodes)",
], ids=["patients", "patient-word-wins", "neutral", "births-are-babies", "no-substring-match", "record-counted"])
def test_graph_count_under_other_names_is_fine(columns):
    query = f"SELECT {columns} WHERE {{ GRAPH ?g {{ ?episode a sphn:AllergyEpisode . }} }}"
    assert hints(check_graph_count_named_as_records, query) == []


def test_record_variable_follows_the_text_even_when_rdflib_reorders_the_triples():
    query = (
        "SELECT ?label (COUNT(DISTINCT ?g) AS ?total_procedures) WHERE { GRAPH ?g { "
        "?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a sphn:BodySite . ?code a ?concept . "
        "?concept rdfs:label ?label . FILTER (LANGMATCHES(LANG(?label), 'de')) } } GROUP BY ?label"
    )
    (hint,) = hints(check_graph_count_named_as_records, query)
    assert "COUNT(DISTINCT ?proc) (the record variable)" in hint


def test_graph_count_aliases_are_resolved_inside_their_own_select():
    query = (
        "SELECT (COUNT(DISTINCT ?prescription) AS ?prescriptions) WHERE { "
        "{ SELECT (COUNT(DISTINCT ?g) AS ?all_prescriptions) WHERE { GRAPH ?g { ?p a sphn:DrugPrescription . } } } "
        "GRAPH ?g { ?prescription a sphn:DrugPrescription . } } GROUP BY ?all_prescriptions"
    )
    assert hints(check_graph_count_named_as_records, query) == [
        "COUNT(DISTINCT ?g) counts patients, but the column is named ?all_prescriptions. Count records with "
        "COUNT(DISTINCT ?p) (the record variable) and patients with COUNT(DISTINCT ?g)."
    ]


# ---------------------------------------------------------------------- 3. duplicate aggregate columns
def test_identical_aggregate_columns_return_the_same_number():
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?total_patients) (COUNT(DISTINCT ?g) AS ?married) "
        "(COUNT(DISTINCT ?g) AS ?single) (1.0 * COUNT(DISTINCT ?g) / ?total_patients AS ?married_rate) "
        "WHERE { GRAPH ?g { ?status a sphn:CivilStatus ; sphn:hasCode ?code . ?code a ?concept . } }"
    )
    assert hints(check_duplicate_aggregate_columns, query) == [
        "columns ?total_patients, ?married, ?single are all COUNT(DISTINCT ?g) and will return the same number. "
        "Count per category instead: ?x sphn:hasCode ?c . ?c a ?concept with VALUES (?concept ?label) { ... } "
        "and GROUP BY ?label."
    ]


@pytest.mark.parametrize("query", [
    "SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?dx) AS ?diagnoses) "
    "WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }",
    "SELECT (COUNT(DISTINCT ?dx) AS ?a) (COUNT(?dx) AS ?b) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }",
    "SELECT (COUNT(DISTINCT ?g) AS ?patients) (AVG(?n) AS ?avg) WHERE { "
    "{ SELECT ?g (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } } GROUP BY ?g } }",
], ids=["different-input", "different-distinct", "different-select"])
def test_distinct_aggregates_are_fine(query):
    assert hints(check_duplicate_aggregate_columns, query) == []


# ---------------------------------------------------------------- 4. unconditional subgroup alias of ?g
def test_subgroup_bound_from_g_on_every_row_equals_the_cohort():
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?total_patients) (COUNT(DISTINCT ?cholera_patient) AS ?cholera_patients) "
        "(ROUND(1000.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 10 AS ?cholera_pct) "
        "WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?dx_code . "
        f"?dx_code a icd-10-gm:A00 {OPTION} . BIND(?g AS ?cholera_patient) }} }}"
    )
    assert hints(check_unconditional_subgroup, query) == [
        "COUNT(DISTINCT ?cholera_patient) is always equal to COUNT(DISTINCT ?g): BIND(?g AS ?cholera_patient) "
        "applies to every row, so the share is 100% by construction. Keep the whole cohort in the first "
        "GRAPH ?g { ... } block and put the subgroup in OPTIONAL { GRAPH ?g { ... } BIND (?g AS ?cholera_patient) }."
    ]


@pytest.mark.parametrize("subgroup", [
    "OPTIONAL { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } BIND (?g AS ?cholera_patient) }",
    "{ GRAPH ?g { ?dx a sphn:BilledDiagnosis . } BIND (?g AS ?cholera_patient) } UNION { GRAPH ?g { ?x a sphn:Death . } }",
], ids=["optional", "union"])
def test_conditional_subgroup_binds_are_fine(subgroup):
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?cholera_patient) AS ?cholera_patients) "
        f"WHERE {{ GRAPH ?g {{ ?sex a sphn:AdministrativeSex . }} {subgroup} }}"
    )
    assert hints(check_unconditional_subgroup, query) == []


# ---------------------------------------------------------------- 5. pseudo-identifier joins and leftovers
PID_JOIN = (
    "SELECT (COUNT(DISTINCT ?drug_patient) AS ?with_drugs) (COUNT(DISTINCT ?ae_patient) AS ?with_events) WHERE { "
    "GRAPH ?g { ?drug_event a sphn:DrugAdministrationEvent . BIND (?g AS ?drug_patient) } "
    "OPTIONAL { GRAPH ?g { ?ae_event a sphn:AdverseEvent ; sphn:hasSubjectPseudoIdentifier ?pid . "
    "?drug_event sphn:hasSubjectPseudoIdentifier ?pid . BIND (?g AS ?ae_patient) } } }"
)


def test_pseudo_identifier_join_between_two_records_is_reported():
    assert hints(check_pseudo_identifier_join, PID_JOIN) == [
        "?pid only joins ?drug_event and ?ae_event on the patient. Records of the same patient are already in "
        "the same GRAPH ?g: remove the sphn:hasSubjectPseudoIdentifier join and match the second record in "
        "GRAPH ?g alone."
    ]
    assert hints(check_unused_pseudo_identifier, PID_JOIN) == []


def test_type_and_identifier_triples_do_not_make_the_join_useful():
    query = (
        "SELECT ?specialty (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?dx a sphn:BilledDiagnosis ; sphn:hasSubjectPseudoIdentifier ?subject . "
        "?subject a sphn:SubjectPseudoIdentifier ; sphn:hasIdentifier ?pseudo_id . "
        "?encounter a sphn:HealthcareEncounter ; sphn:hasSubjectPseudoIdentifier ?subject ; "
        "sphn:hasTherapeuticArea ?area . ?area sphn:hasSpecialtyName ?specialty . } } GROUP BY ?specialty"
    )
    (hint,) = hints(check_pseudo_identifier_join, query)
    assert hint.startswith("?subject only joins ?dx and ?encounter on the patient.")


def test_unused_pseudo_identifier_triple_is_reported_by_its_own_check():
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(?dx) AS ?diagnoses) WHERE { GRAPH ?g { "
        "?dx a sphn:BilledDiagnosis ; sphn:hasSubjectPseudoIdentifier ?pseudo_id ; sphn:hasCode ?dx_code . "
        f"?dx_code a icd-10-gm:A00 {OPTION} . }} }}"
    )
    assert hints(check_unused_pseudo_identifier, query) == [
        "?pseudo_id (sphn:hasSubjectPseudoIdentifier of ?dx) is never used: remove that triple. Records of the "
        "same patient are already in the same GRAPH ?g."
    ]
    assert hints(check_pseudo_identifier_join, query) == []


@pytest.mark.parametrize("query", [
    "SELECT ?pid WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasSubjectPseudoIdentifier ?pid . } }",
    "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?sex a sphn:AdministrativeSex . } OPTIONAL { "
    "GRAPH ?g { ?p a sphn:DrugPrescription ; sphn:hasSubjectPseudoIdentifier ?pid . } FILTER (BOUND(?pid)) } }",
    "SELECT ?id WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasSubjectPseudoIdentifier ?pid . "
    "?pid sphn:hasIdentifier ?id . } }",
    "SELECT (COUNT(DISTINCT ?pid) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; "
    "sphn:hasSubjectPseudoIdentifier ?pid . } }",
    "SELECT ?p WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasSubjectPseudoIdentifier ?pid . "
    "?pid sphn:hasDataProvider ?p . } }",
], ids=["projected", "filtered-inside-optional", "identifier-read", "aggregated", "other-triple"])
def test_pseudo_identifiers_that_are_read_are_fine(query):
    assert hints(check_pseudo_identifier_join, query) == []
    assert hints(check_unused_pseudo_identifier, query) == []


# ------------------------------------------------------------ 6. code node compared with a concept IRI
def test_code_node_compared_with_concepts_gets_the_optional_bind_pattern():
    query = (
        "SELECT ?concept (COUNT(DISTINCT ?accepted_patient) AS ?accepted) WHERE { GRAPH ?g { "
        "?consent a sphn:Consent ; sphn:hasStatusCode ?status_code . ?status_code a ?concept . } "
        "BIND (IF(?status_code = snomed:385645004, ?g, ?accepted_patient) AS ?accepted_patient) "
        "BIND (IF(?status_code = snomed:443390004, ?g, ?refused_patient) AS ?refused_patient) } GROUP BY ?concept"
    )
    assert hints(check_code_node_compared_with_concept, query) == [
        "?status_code is a code node (object of sphn:hasStatusCode): one node per record, never equal to the "
        "concept snomed:385645004, snomed:443390004. Test its class instead, one OPTIONAL per concept: "
        "OPTIONAL { ?status_code a snomed:385645004 . BIND (?g AS ?accepted_patient) } and "
        "COUNT(DISTINCT ?accepted_patient)."
    ]


def test_code_node_in_a_filter_with_an_icd_code_adds_the_inference_option():
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . "
        "FILTER (?code IN (icd-10-gm:A00, icd-10-gm:A01)) } }"
    )
    assert hints(check_code_node_compared_with_concept, query) == [
        "?code is a code node (object of sphn:hasCode): one node per record, never equal to the concept "
        f"icd-10-gm:A00. Test its class instead: OPTIONAL {{ ?code a icd-10-gm:A00 {OPTION} . "
        "BIND (?g AS ?matched) } and COUNT(DISTINCT ?matched)."
    ]


def test_comparing_the_concept_or_two_code_nodes_is_fine():
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . "
        "?code a ?concept . ?other sphn:hasCode ?code2 . FILTER (?concept = snomed:385645004 && ?code != ?code2) } }"
    )
    assert hints(check_code_node_compared_with_concept, query) == []


# ------------------------------------------------------------- 7. VALUES-bound codes without inference
VALUES_CODES = (
    "SELECT ?cholera_type (COUNT(DISTINCT ?dx) AS ?diagnoses) WHERE { GRAPH ?g { "
    "?dx a sphn:BilledDiagnosis ; sphn:hasCode ?dx_code . ?dx_code a ?concept OPTION_SLOT . "
    'VALUES (?concept ?cholera_type) { (icd-10-gm:A00.0 "classical") (icd-10-gm:A00.1 "El Tor") } } } '
    "GROUP BY ?cholera_type"
)


def test_values_bound_codes_need_the_inference_option():
    assert hints(check_values_codes_without_inference, VALUES_CODES.replace("OPTION_SLOT", "")) == [
        "VALUES binds ?concept to ICD-10-GM/CHOP codes, whose code nodes are typed with year-versioned subcodes: "
        f"write ?dx_code a ?concept {OPTION} or only the few year-less code nodes match."
    ]
    assert hints(check_values_codes_without_inference, VALUES_CODES.replace("OPTION_SLOT", OPTION)) == []


def test_values_of_snomed_concepts_need_no_option():
    query = VALUES_CODES.replace("OPTION_SLOT", "").replace("icd-10-gm:A00.0", "snomed:1").replace("icd-10-gm:A00.1", "snomed:2")
    assert hints(check_values_codes_without_inference, query) == []


# --------------------------------------------------------------------------- 8. non-numeric SNOMED IRI
def test_non_numeric_snomed_iri_is_not_a_concept():
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?birth a sphn:Birth ; sphn:hasCountry ?country . "
        f"?country a snomed:Country {OPTION} . ?birth sphn:hasCode ?c . ?c a snomed:223369002 . }} }}"
    )
    assert hints(check_fake_snomed_iri, query) == [
        "snomed:Country is not a SNOMED CT concept id; concept ids are numeric, e.g. snomed:223369002 from the "
        "code candidates."
    ]


def test_schema_cards_are_loaded_once():
    assert semantics._cards() is semantics._cards()
    assert "Death" in semantics._cards().classes
