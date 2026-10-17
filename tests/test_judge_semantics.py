import json

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.context import SchemaCards
from nl2sparql.judge_checks import semantics
from nl2sparql.judge_checks.semantics import (
    CHECKS,
    REWRITES,
    check_backbone_patient_count,
    check_code_node_compared_with_concept,
    check_date_function_on_record,
    check_duplicate_aggregate_columns,
    check_fake_snomed_iri,
    check_graph_count_named_as_records,
    check_group_key_from_unlinked_optional,
    check_percentage_scale,
    check_pseudo_identifier_join,
    check_required_count_tested_positive,
    check_scaled_statistic,
    check_unconditional_subgroup,
    check_unused_pseudo_identifier,
    check_values_codes_without_inference,
    check_year_constant_on_value,
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
    """The official and team examples and the dev reference queries; holdout rows are a blind exam and stay closed."""
    queries = [(row["id"], row["sparql"]) for row in load_examples()]
    for line in (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            queries.append((row["id"], row["sparql"]))
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
        check_scaled_statistic, check_percentage_scale, check_year_constant_on_value,
        check_required_count_tested_positive, check_backbone_patient_count, check_group_key_from_unlinked_optional,
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


def test_year_on_a_date_parts_record_offers_only_the_linking_records_datetime():
    # q-005: the shipped hint offered DeathDate.hasYear first; those parts are random in the data and the
    # retry that followed it returned no rows. Only the linking record's dateTime is offered now.
    query = (
        "SELECT ?year (COUNT(DISTINCT ?g) AS ?deaths) WHERE { GRAPH ?g { "
        "?death a sphn:Death ; sphn:hasDate ?death_date . ?death_date a sphn:DeathDate ; sphn:hasYear ?year . } "
        "FILTER (YEAR(?death_date) >= 2015 && YEAR(?death_date) <= 2025) } GROUP BY ?year"
    )
    assert hints(check_date_function_on_record, query) == [
        "?death_date is a sphn:DeathDate record, not a date, and its sphn:hasYear/hasMonth/hasDay parts hold no "
        "calendar values in this data: use the dateTime of the record that links to it, ?death sphn:hasReportDateTime "
        "?death_report, and YEAR(?death_report)."
    ]
    alone = "SELECT ?y WHERE { GRAPH ?g { ?death_date a sphn:DeathDate . } BIND (YEAR(?death_date) AS ?y) }"
    assert hints(check_date_function_on_record, alone) == [
        "?death_date is a sphn:DeathDate record, not a date, and its sphn:hasYear/hasMonth/hasDay parts hold no "
        "calendar values in this data: use the dateTime of the record that links to it, ?death a sphn:Death ; "
        "sphn:hasDate ?death_date ; sphn:hasReportDateTime ?death_report, and YEAR(?death_report)."
    ]
    birth = "SELECT ?y WHERE { GRAPH ?g { ?birth sphn:hasDate ?bd . ?bd a sphn:BirthDate . } BIND (YEAR(?bd) AS ?y) }"
    (hint,) = hints(check_date_function_on_record, birth)
    assert hint.endswith(
        "no record linking to it carries a dateTime, so this data has no usable date for it; for ages use "
        "?age a sphn:Age ; sphn:hasQuantity ?quantity . ?quantity sphn:hasValue ?age_value."
    )
    assert "hasYear ?year" not in hint


def test_year_over_virtuosos_year_function_needs_a_datetime():
    # q-054's dropped retry: YEAR(bif:year(?start)) fails with DT001, and the date check never saw it.
    query = (
        "SELECT (YEAR(bif:year(?start)) AS ?year) (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { "
        "?proc a sphn:BilledProcedure ; sphn:hasStartDateTime ?start . } } GROUP BY (YEAR(bif:year(?start)))"
    )
    assert hints(check_date_function_on_record, query) == [
        "YEAR(bif:year(?start)): bif:year already returns a number, but YEAR() needs a dateTime (Virtuoso DT001): "
        "write YEAR(?start)."
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


def test_record_hidden_in_a_sub_select_is_asked_to_be_exported():
    # q-003: the hint named ?case although the sub-select does not project it; the retry wrote SELECT ?stays
    # with nothing binding it, in 3 of 3 runs.
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?stays) (AVG(?days) AS ?avg_days) WHERE { "
        "{ SELECT DISTINCT ?g ?admission ?discharge WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase ; "
        "sphn:hasAdmissionDateTime ?admission ; sphn:hasDischargeDateTime ?discharge . } } } "
        'BIND (bif:datediff("day", ?admission, ?discharge) AS ?days) }'
    )
    assert hints(check_graph_count_named_as_records, query) == [
        "COUNT(DISTINCT ?g) counts patients, but the column is named ?stays. Count records with COUNT(DISTINCT ?case) "
        "(the record variable: add ?case to the sub-select's SELECT) and patients with COUNT(DISTINCT ?g)."
    ]
    # A record the SELECT can read wins over the first typed record of the query.
    visible = (
        "SELECT (COUNT(DISTINCT ?g) AS ?stays) WHERE { "
        "{ SELECT ?g WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } } } "
        "GRAPH ?g { ?case a sphn:AdministrativeCase . } }"
    )
    assert hints(check_graph_count_named_as_records, visible) == [
        "COUNT(DISTINCT ?g) counts patients, but the column is named ?stays. Count records with COUNT(DISTINCT ?case) "
        "(the record variable) and patients with COUNT(DISTINCT ?g)."
    ]


# ---------------------------------------------------------------------- 3. duplicate aggregate columns
def test_identical_aggregate_columns_get_the_querys_own_category_pattern():
    # No concrete code in the query: the VALUES rows are left to the code candidates, but the record, the code
    # property and the code node are the query's own, never a '?x sphn:hasCode ?c' template.
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?total_patients) (COUNT(DISTINCT ?g) AS ?married) "
        "(COUNT(DISTINCT ?g) AS ?single) (1.0 * COUNT(DISTINCT ?g) / ?total_patients AS ?married_rate) "
        "WHERE { GRAPH ?g { ?status a sphn:CivilStatus ; sphn:hasCode ?code . ?code a ?concept . } }"
    )
    assert hints(check_duplicate_aggregate_columns, query) == [
        "columns ?total_patients, ?married, ?single are all COUNT(DISTINCT ?g) and will return the same number. "
        "Count per category instead: ?status a sphn:CivilStatus ; sphn:hasCode ?code . ?code a ?concept . "
        'VALUES (?concept ?label) { (<concept> "label") ... one row per column, from the code candidates } and '
        "GROUP BY ?label, with one (COUNT(DISTINCT ?g) AS ?patients) column."
    ]


def test_duplicate_columns_over_typed_codes_list_the_codes_with_their_labels(monkeypatch):
    # q-047: the shipped template replaced sphn:hasAcquistionCode by sphn:hasCode and dropped sphn:Nationality.
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    cards.value_sets["Nationality"] = {"hasAcquistionCode": [
        {"value": "snomed:19535007", "label": "Original"}, {"value": "snomed:255396000", "label": "Acquired"},
    ]}
    monkeypatch.setattr(semantics, "_cards", lambda: cards)
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?original_nationality_patients) (COUNT(DISTINCT ?g) AS ?acquired_nationality_patients) "
        "WHERE { { SELECT ?g WHERE { GRAPH ?g { ?nat a sphn:Nationality ; sphn:hasAcquistionCode ?code . "
        f"?code a snomed:19535007 {OPTION} . }} }} }} UNION {{ SELECT ?g WHERE {{ GRAPH ?g {{ "
        f"?nat a sphn:Nationality ; sphn:hasAcquistionCode ?code . ?code a snomed:255396000 {OPTION} . }} }} }} }}"
    )
    assert hints(check_duplicate_aggregate_columns, query) == [
        "columns ?original_nationality_patients, ?acquired_nationality_patients are all COUNT(DISTINCT ?g) and will "
        "return the same number. Count per category instead: ?nat a sphn:Nationality ; sphn:hasAcquistionCode ?code . "
        '?code a ?concept . VALUES (?concept ?label) { (snomed:19535007 "Original") (snomed:255396000 "Acquired") } '
        "and GROUP BY ?label, with one (COUNT(DISTINCT ?g) AS ?patients) column."
    ]
    icd = query.replace("snomed:19535007", "icd-10-gm:A00.0").replace("snomed:255396000", "icd-10-gm:A00.1")
    (hint,) = hints(check_duplicate_aggregate_columns, icd)
    assert f'?code a ?concept {OPTION} . VALUES (?concept ?label) {{ (icd-10-gm:A00.0 "...") (icd-10-gm:A00.1 "...") }}' in hint


def test_duplicate_columns_next_to_a_per_patient_count_get_the_threshold_sum():
    # q-035: the retry that followed the category template invented icd-10-gm:E11; SUM(IF) gives the exact answer.
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?total_patients) (AVG(?count_diagnoses) AS ?avg_diagnoses) "
        "(MAX(?count_diagnoses) AS ?max_diagnoses) (COUNT(DISTINCT ?g) AS ?patients_with_more_than_one) WHERE { "
        "{ SELECT ?g (COUNT(DISTINCT ?diagnosis) AS ?count_diagnoses) WHERE { GRAPH ?g { ?diagnosis a sphn:BilledDiagnosis . } } "
        "GROUP BY ?g } }"
    )
    assert hints(check_duplicate_aggregate_columns, query) == [
        "columns ?total_patients, ?patients_with_more_than_one are all COUNT(DISTINCT ?g) and will return the same "
        "number. Keep COUNT(DISTINCT ?g) for the total and count the patients above the threshold: "
        "SUM(IF(?count_diagnoses > 1, 1, 0)) AS ?patients_with_more_than_one."
    ]
    least = query.replace("?patients_with_more_than_one", "?patients_with_at_least_3").replace("MAX(", "MIN(")
    (hint,) = hints(check_duplicate_aggregate_columns, least)
    assert hint.endswith("SUM(IF(?count_diagnoses >= 3, 1, 0)) AS ?patients_with_at_least_3.")
    fixed = query.replace("(COUNT(DISTINCT ?g) AS ?patients_with_more_than_one)", "(SUM(IF(?count_diagnoses > 1, 1, 0)) AS ?patients_with_more_than_one)")
    assert hints(check_duplicate_aggregate_columns, fixed) == []


def test_duplicate_columns_without_a_code_node_get_one_condition_per_column():
    query = "SELECT (COUNT(DISTINCT ?g) AS ?with_a) (COUNT(DISTINCT ?g) AS ?with_b) WHERE { GRAPH ?g { ?x a sphn:Death . } }"
    assert hints(check_duplicate_aggregate_columns, query) == [
        "columns ?with_a, ?with_b are all COUNT(DISTINCT ?g) and will return the same number. Each column needs its "
        "own condition: OPTIONAL { GRAPH ?g { <condition for ?with_a> } BIND(?g AS ?with_a_patient) } and "
        "COUNT(DISTINCT ?with_a_patient); OPTIONAL { GRAPH ?g { <condition for ?with_b> } BIND(?g AS ?with_b_patient) } "
        "and COUNT(DISTINCT ?with_b_patient)."
    ]


def test_duplicate_columns_are_left_to_the_record_name_hint_when_one_counts_records():
    # q-052: ?prescriptions and ?patients are both COUNT(DISTINCT ?g); the record-name hint explains the difference,
    # and the category advice sent the retry after a category nobody asked for.
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?prescription a sphn:DrugPrescription ; sphn:hasIndicationToStart ?indication . } }"
    )
    assert hints(check_duplicate_aggregate_columns, query) == []
    assert len(hints(check_graph_count_named_as_records, query)) == 1


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


def test_code_node_that_labels_a_group_key_gets_the_values_with_the_querys_labels():
    # q-050: the subgroup template, applied to a GROUP BY label, left an empty group in 3 of 3 runs.
    query = (
        "SELECT ?cholera_type (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; "
        f"sphn:hasCode ?dx_code . ?dx_code a icd-10-gm:A00 {OPTION} . }} "
        'BIND (IF(?dx_code = icd-10-gm:A00.0, "classical", IF(?dx_code = icd-10-gm:A00.1, "El Tor", '
        'IF(?dx_code = icd-10-gm:A00.9, "unspecified", "other"))) AS ?cholera_type) } GROUP BY ?cholera_type'
    )
    assert hints(check_code_node_compared_with_concept, query) == [
        "?dx_code is a code node (object of sphn:hasCode): one node per record, never equal to the concept "
        "icd-10-gm:A00.0, icd-10-gm:A00.1, icd-10-gm:A00.9. Bind ?cholera_type from its class instead: "
        'VALUES (?concept ?cholera_type) { (icd-10-gm:A00.0 "classical") (icd-10-gm:A00.1 "El Tor") '
        f'(icd-10-gm:A00.9 "unspecified") }} ?dx_code a ?concept {OPTION} . and GROUP BY ?cholera_type.'
    ]
    typed = query.replace("?dx_code a icd-10-gm:A00 " + OPTION + " .", f"?dx_code a ?cls {OPTION} .")
    (hint,) = hints(check_code_node_compared_with_concept, typed)
    assert "VALUES (?cls ?cholera_type)" in hint and "?dx_code a ?cls " in hint


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


def test_the_bare_snomed_namespace_in_a_filter_is_not_a_fake_concept():
    query = (
        "SELECT ?side WHERE { GRAPH ?g { ?site sphn:hasCode ?site_code . ?site_code a ?site_concept . } "
        "FILTER (STRSTARTS(STR(?site_concept), STR(snomed:))) BIND (REPLACE(STR(?site_concept), \"^.*/\", \"\") AS ?side) }"
    )
    assert hints(check_fake_snomed_iri, query) == []


def test_schema_cards_are_loaded_once():
    assert semantics._cards() is semantics._cards()
    assert "Death" in semantics._cards().classes


# ---------------------------------------------------------------------------- 9. scaled statistics
def test_statistic_scaled_like_a_percentage_is_named_with_its_own_expression():
    # q-032 and q-064: the percentage formula ROUND(1000.0 * x) / 10 applied to an average and a maximum.
    query = (
        "SELECT (ROUND(1000.0 * AVG(?hours)) / 10 AS ?avg_hours) (ROUND(1000.0 * MAX(?hours)) / 10 AS ?max_hours) "
        "(COUNT(DISTINCT ?encounter) AS ?encounters) WHERE { GRAPH ?g { ?encounter a sphn:HealthcareEncounter ; "
        'sphn:hasStartDateTime ?start ; sphn:hasEndDateTime ?end . } BIND (bif:datediff("hour", ?start, ?end) AS ?hours) }'
    )
    assert hints(check_scaled_statistic, query) == [
        "?avg_hours = ROUND(1000.0 * AVG(?hours)) / 10 is 100 x the average; write ROUND(10 * AVG(?hours)) / 10 AS ?avg_hours.",
        "?max_hours = ROUND(1000.0 * MAX(?hours)) / 10 is 100 x the maximum; write MAX(?hours) AS ?max_hours.",
    ]
    ratio = (
        "SELECT (ROUND(1000.0 * COUNT(DISTINCT ?dx) / COUNT(DISTINCT ?g)) / 10 AS ?diagnoses_per_patient) "
        "WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }"
    )
    assert hints(check_scaled_statistic, ratio) == [
        "?diagnoses_per_patient = ROUND(1000.0 * COUNT(DISTINCT ?dx) / COUNT(DISTINCT ?g)) / 10 is 100 x the ratio; "
        "write ROUND(10 * COUNT(DISTINCT ?dx) / COUNT(DISTINCT ?g)) / 10 AS ?diagnoses_per_patient."
    ]


@pytest.mark.parametrize("column", [
    "(ROUND(1000.0 * COUNT(DISTINCT ?dx) / COUNT(DISTINCT ?g)) / 10 AS ?cholera_pct)",
    "(ROUND(1000.0 * COUNT(DISTINCT ?dx) / COUNT(DISTINCT ?g)) / 10 AS ?share_of_patients)",
    "(1000.0 * COUNT(DISTINCT ?dx) / COUNT(DISTINCT ?g) AS ?diagnoses_per_1000)",
    "(ROUND(10 * AVG(?v)) / 10 AS ?avg_value)",
    "(ROUND(100 * AVG(?v)) / 100 AS ?avg_value)",
    "(AVG(?v) * 24 AS ?avg_hours)",
    "(ROUND(1000.0 * 2) / 10 AS ?constant)",
], ids=["pct", "share", "per-thousand", "one-decimal", "two-decimals", "not-a-power-of-ten", "constants-only"])
def test_percentages_rates_and_rounding_to_decimals_are_not_scaled_statistics(column):
    query = f"SELECT {column} WHERE {{ GRAPH ?g {{ ?dx a sphn:BilledDiagnosis ; sphn:hasValue ?v . }} }}"
    assert hints(check_scaled_statistic, query) == []


def test_percentage_whose_constants_do_not_make_100_is_corrected():
    # q-013: ROUND(100.0 * a / b) / 100 is the fraction, not the percentage, in all three attempts.
    query = (
        "SELECT (ROUND(100.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 100 AS ?cholera_pct) "
        "WHERE { GRAPH ?g { ?sex a sphn:AdministrativeSex . } OPTIONAL { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } "
        "BIND (?g AS ?cholera_patient) } }"
    )
    assert hints(check_percentage_scale, query) == [
        "?cholera_pct = ROUND(100.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 100 scales the ratio by 1, "
        "not 100: a percentage is ROUND(1000.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 10 AS ?cholera_pct."
    ]
    ten = query.replace("/ 100 AS ?cholera_pct", "/ 10 AS ?cholera_pct")
    assert hints(check_percentage_scale, ten)[0].startswith("?cholera_pct = ROUND(100.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 10 scales the ratio by 10, not 100")
    for right in ("ROUND(1000.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 10", "100.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)"):
        assert hints(check_percentage_scale, query.replace("ROUND(100.0 * COUNT(DISTINCT ?cholera_patient) / COUNT(DISTINCT ?g)) / 100", right)) == []
    no_division = "SELECT (ROUND(10 * AVG(?v)) / 10 AS ?share) WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasValue ?v . } }"
    assert hints(check_percentage_scale, no_division) == []


# ------------------------------------------------------------------------ 10. calendar years on values
def test_decade_computed_from_a_measured_value_but_compared_with_years():
    # q-059: team-ex-03's age decades were copied for a question about calendar decades.
    query = (
        "SELECT ?decade (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?age a sphn:Age ; "
        "sphn:hasQuantity/sphn:hasValue ?age_value . ?adverse_event a sphn:AdverseEvent ; sphn:hasOnsetDateTime ?onset . } "
        "BIND (10 * FLOOR(?age_value / 10) AS ?decade) FILTER (?decade >= 1970 && ?decade <= 2010) } GROUP BY ?decade"
    )
    assert hints(check_year_constant_on_value, query) == [
        "?decade is computed from ?age_value (sphn:hasValue, a measured value) but compared with the year 1970: "
        "compute it from a date instead, BIND(10 * FLOOR(YEAR(?onset) / 10) AS ?decade)."
    ]
    # The value may reach the key through a chain of BINDs; the remedy rewrites the key's own BIND.
    chained = query.replace("BIND (10 * FLOOR(?age_value / 10) AS ?decade)", "BIND (?age_value / 10 AS ?tenth) BIND (10 * FLOOR(?tenth) AS ?decade)")
    assert hints(check_year_constant_on_value, chained) == [
        "?decade is computed from ?age_value (sphn:hasValue, a measured value) but compared with the year 1970: "
        "compute it from a date instead, BIND(10 * FLOOR(?tenth) AS ?decade)."
    ]


@pytest.mark.parametrize("query", [
    "SELECT ?decade WHERE { GRAPH ?g { ?e a sphn:AdverseEvent ; sphn:hasOnsetDateTime ?onset . } "
    "BIND (10 * FLOOR(YEAR(?onset) / 10) AS ?decade) FILTER (?decade >= 1970) }",
    "SELECT ?bucket WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasQuantity ?q . ?q sphn:hasValue ?v . } "
    "BIND (10 * FLOOR(?v / 10) AS ?bucket) FILTER (?bucket >= 60) }",
    "SELECT ?v WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasQuantity ?q . ?q sphn:hasValue ?v . } FILTER (?v > 1900) }",
], ids=["from-a-date", "not-a-year", "the-value-itself"])
def test_year_buckets_from_dates_and_plain_thresholds_are_fine(query):
    assert hints(check_year_constant_on_value, query) == []


# ------------------------------------------------------------------- 11. counts of a required record
WITH_PRESCRIPTIONS = (
    "SELECT ?cohort (COUNT(DISTINCT ?g) AS ?patients) (SUM(IF(?prescriptions > 0, 1, 0)) AS ?with_prescription) "
    "WHERE { { SELECT ?g (COUNT(DISTINCT ?drug_prescription) AS ?prescriptions) WHERE { GRAPH ?g { "
    "?drug_prescription a sphn:DrugPrescription . } } GROUP BY ?g } "
    'BIND (IF(EXISTS { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }, "with cholera", "other") AS ?cohort) } GROUP BY ?cohort'
)


def test_share_of_patients_with_a_required_record_is_100_percent_by_construction():
    # q-068: the sub-select requires a prescription, so SUM(IF(?prescriptions > 0, 1, 0)) is the patient count.
    assert hints(check_required_count_tested_positive, WITH_PRESCRIPTIONS) == [
        "SUM(IF(?prescriptions > 0, 1, 0)) always equals COUNT(DISTINCT ?g): the sub-select counts ?drug_prescription, "
        "which it requires, so it keeps only patients who have a sphn:DrugPrescription. Start it from every patient "
        "(GRAPH ?g { ?sex a sphn:AdministrativeSex }) with the ?drug_prescription records in OPTIONAL."
    ]
    for test in ("?prescriptions >= 1", "?prescriptions != 0", "0 < ?prescriptions"):
        assert len(hints(check_required_count_tested_positive, WITH_PRESCRIPTIONS.replace("?prescriptions > 0", test))) == 1
    cohort = WITH_PRESCRIPTIONS.replace(
        "(SUM(IF(?prescriptions > 0, 1, 0)) AS ?with_prescription)", ""
    ).replace("BIND (IF(EXISTS", 'BIND (IF(?prescriptions > 0, "with", "without") AS ?has) BIND (IF(EXISTS').replace("GROUP BY ?cohort", "GROUP BY ?cohort ?has").replace("SELECT ?cohort", "SELECT ?cohort ?has")
    (hint,) = hints(check_required_count_tested_positive, cohort)
    assert hint.startswith("?prescriptions > 0 holds for every patient: the sub-select counts ?drug_prescription")


def test_counts_over_optional_records_and_plain_filters_are_fine():
    backbone = WITH_PRESCRIPTIONS.replace(
        "GRAPH ?g { ?drug_prescription a sphn:DrugPrescription . }",
        "GRAPH ?g { ?sex a sphn:AdministrativeSex . } OPTIONAL { GRAPH ?g { ?drug_prescription a sphn:DrugPrescription . } }",
    )
    assert hints(check_required_count_tested_positive, backbone) == []
    filtered = WITH_PRESCRIPTIONS.replace("(SUM(IF(?prescriptions > 0, 1, 0)) AS ?with_prescription)", "(AVG(?prescriptions) AS ?avg)").replace(
        "GROUP BY ?g }", "GROUP BY ?g } FILTER (?prescriptions > 0)"
    )
    assert hints(check_required_count_tested_positive, filtered) == []
    threshold = WITH_PRESCRIPTIONS.replace("?prescriptions > 0", "?prescriptions > 3")
    assert hints(check_required_count_tested_positive, threshold) == []


# ----------------------------------------------------------- 12. patients counted over the backbone
IMPLANTS = (
    "SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?implant_presence) AS ?implant_episodes) "
    "(AVG(?days) AS ?avg_days) WHERE { GRAPH ?g { ?sex_record a sphn:AdministrativeSex ; sphn:hasCode ?sex_code . "
    '?sex_code a ?sex_concept . } VALUES (?sex_concept ?sex) { (snomed:248153007 "male") } '
    "OPTIONAL { GRAPH ?g { ?implant_presence a sphn:ImplantPresence ; sphn:hasStartDateTime ?start ; "
    "sphn:hasEndDateTime ?end . BIND (bif:datediff('day', ?start, ?end) AS ?days) } } } GROUP BY ?sex"
)


def test_patient_count_over_the_backbone_while_the_measures_come_from_one_optional():
    # q-069: patients per sex, not patients with an implant.
    assert hints(check_backbone_patient_count, IMPLANTS) == [
        "COUNT(DISTINCT ?g) counts every patient of the group, also those without sphn:ImplantPresence (?days, "
        "?implant_presence are bound only inside OPTIONAL): make that block required, or count the patients that have "
        "one with BIND(?g AS ?implant_presence_patient) inside it and COUNT(DISTINCT ?implant_presence_patient)."
    ]
    required = IMPLANTS.replace("OPTIONAL { GRAPH ?g { ?implant_presence", "GRAPH ?g { ?implant_presence").replace("AS ?days) } } }", "AS ?days) } }")
    assert hints(check_backbone_patient_count, required) == []


@pytest.mark.parametrize("columns, block", [
    ("(COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?implant_patient) AS ?with_implant)",
     "OPTIONAL { GRAPH ?g { ?implant_presence a sphn:ImplantPresence . } BIND (?g AS ?implant_patient) }"),
    ("(COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?case_with_proc) AS ?stays_with_procedure)",
     "GRAPH ?g { ?case a sphn:AdministrativeCase . } OPTIONAL { GRAPH ?g { ?proc a sphn:BilledProcedure ; "
     "sphn:hasAdministrativeCase ?case . } BIND (?case AS ?case_with_proc) }"),
    ("(COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?h) AS ?others)",
     "OPTIONAL { GRAPH ?h { ?implant_presence a sphn:ImplantPresence . } }"),
], ids=["patient-flag", "record-flag", "graph-variable"])
def test_conditional_counts_over_flags_and_graph_variables_are_fine(columns, block):
    query = (
        f"SELECT {columns} WHERE {{ GRAPH ?g {{ ?sex_record a sphn:AdministrativeSex . }} {block} }}"
    )
    assert hints(check_backbone_patient_count, query) == []


# ------------------------------------------------------- 13. group key bound in an unlinked OPTIONAL
SIDES = (
    "SELECT ?side (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . "
    f"?code a chop:00.0 {OPTION} . "
    'OPTIONAL { GRAPH ?g { ?bodySite a sphn:BodySite ; sphn:hasCode ?bodyCode . ?bodyCode a snomed:7771000 . BIND ("Left" AS ?side) } } '
    'OPTIONAL { GRAPH ?g { ?bodySite a sphn:BodySite ; sphn:hasCode ?bodyCode . ?bodyCode a snomed:24028007 . BIND ("Right" AS ?side) } } '
    "} } GROUP BY ?side"
)


def test_group_key_bound_only_in_an_optional_linked_by_the_patient_alone():
    # q-055: the body sites are not linked to the procedure, so every procedure meets every body site of the patient.
    assert hints(check_group_key_from_unlinked_optional, SIDES) == [
        "GROUP BY ?side: ?side is bound only inside an OPTIONAL block that shares no variable with ?proc (only the "
        "patient ?g), so every row is paired with every sphn:BodySite of the patient and the rows without one form an "
        "extra group with an empty ?side. Link the block to the record, ?proc sphn:hasBodySite ?bodySite, and bind "
        "?side on every row (VALUES (?concept ?side) { ... } on its code, or FILTER(BOUND(?side)))."
    ]
    linked = SIDES.replace("?bodySite a sphn:BodySite ;", "?proc sphn:hasBodySite ?bodySite . ?bodySite a sphn:BodySite ;")
    assert hints(check_group_key_from_unlinked_optional, linked) == []


def test_patient_flags_and_blocks_on_hidden_variables_are_not_unlinked_keys():
    flag = (
        "SELECT ?cohort (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?sex a sphn:AdministrativeSex . } "
        "OPTIONAL { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } BIND (?g AS ?cohort) } } GROUP BY ?cohort"
    )
    assert hints(check_group_key_from_unlinked_optional, flag) == []
    # q-074: the OPTIONAL joins on ?proc and ?start, which the sub-select hides; that is the scope checks' hint.
    hidden = (
        "SELECT ?year (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g (COUNT(DISTINCT ?proc) AS ?proc_count) WHERE { "
        "GRAPH ?g { ?proc a sphn:ImagingProcedure ; sphn:hasStartDateTime ?start . } } GROUP BY ?g } "
        "OPTIONAL { GRAPH ?g { ?proc sphn:hasIntent ?intent . ?intent a sphn:Intent . } BIND (YEAR(?start) AS ?year) } } "
        "GROUP BY ?year"
    )
    assert hints(check_group_key_from_unlinked_optional, hidden) == []
