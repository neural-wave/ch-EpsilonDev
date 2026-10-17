import json

import pytest

from conftest import DATA_DIR, PREFIXES, load_examples
from nl2sparql.judge_checks.scope import (
    CHECKS,
    check_aggregate_over_aggregate,
    check_filter_restricts_statistic,
    check_optional_cross_product,
    check_rebound_variables,
    check_unbound_variables,
)
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes

EVAL_DIR = DATA_DIR.parent / "eval"
SEX = (
    "GRAPH ?g { ?sex_record a sphn:AdministrativeSex ; sphn:hasCode ?sex_code . ?sex_code a ?sex_concept } "
    'VALUES (?sex_concept ?sex) { (snomed:248153007 "male") (snomed:248152002 "female") } '
)
PER_PATIENT_DIAGNOSES = (
    "{ SELECT ?g (COUNT(DISTINCT ?dx) AS ?diagnoses) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY ?g } "
)


def parse(query: str):
    return parse_sparql(add_missing_prefixes(PREFIXES + query + "\n"))


def load_dev_references() -> list[dict]:
    """The dev rows only: the holdout rows are a blind exam and are never read."""
    lines = (EVAL_DIR / "query_questions.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return [row for row in rows if row["split"] == "dev"]


# ---------------------------------------------------------------- unbound variables

def test_group_key_that_nothing_binds_is_reported_with_every_clause_that_reads_it():
    query = ("SELECT ?year (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?p a sphn:BilledProcedure ; "
             "sphn:hasStartDateTime ?start } } GROUP BY ?year ORDER BY ?year")
    assert check_unbound_variables(parse(query)) == [
        "?year is used in SELECT/GROUP BY/ORDER BY but nothing binds it: "
        "add BIND(<expression> AS ?year) or a pattern that binds it."
    ]
    bound = query.replace("} } GROUP BY", "} BIND(YEAR(?start) AS ?year) } GROUP BY")
    assert check_unbound_variables(parse(bound)) == []


def test_sub_select_exports_only_the_variables_it_projects():
    query = ("SELECT ?cohort (AVG(?days) AS ?avg_days) WHERE { { SELECT ?g (AVG(?days) AS ?per_patient) "
             "WHERE { GRAPH ?g { ?a a sphn:DrugAdministrationEvent ; sphn:hasDuration ?q . ?q sphn:hasValue ?days } } "
             "GROUP BY ?g } } GROUP BY ?cohort")
    assert check_unbound_variables(parse(query)) == [
        "?cohort is used in SELECT/GROUP BY but nothing binds it: add BIND(<expression> AS ?cohort) or a pattern that binds it.",
        "AVG(?days): ?days is not bound in this scope (a sub-select only exports the variables it projects).",
    ]


def test_sub_select_that_projects_what_nothing_binds_is_reported_in_its_own_scope():
    query = ("SELECT ?delivery_mode (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g ?delivery_mode WHERE { GRAPH ?g { "
             "?birth a sphn:Birth ; sphn:hasDeliveryModeCode ?code } } GROUP BY ?g ?delivery_mode } } GROUP BY ?delivery_mode")
    assert check_unbound_variables(parse(query)) == [
        "?delivery_mode is used in SELECT/GROUP BY but nothing binds it: "
        "add BIND(<expression> AS ?delivery_mode) or a pattern that binds it."
    ]


def test_per_patient_statistic_without_its_sub_select_gets_the_sketch_with_the_record_class():
    query = (f"SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) (AVG(?prescriptions_per_patient) AS ?avg) "
             f"(MAX(?prescriptions_per_patient) AS ?max) WHERE {{ {SEX} "
             "OPTIONAL { GRAPH ?g { ?prescription a sphn:DrugPrescription } } } GROUP BY ?sex")
    sketch = ("Per-patient statistics need a subquery: { SELECT ?g (COUNT(DISTINCT ?prescription) AS "
              "?prescriptions_per_patient) WHERE { GRAPH ?g { ?prescription a sphn:DrugPrescription } } GROUP BY ?g } "
              "then AVG(?prescriptions_per_patient), MAX(?prescriptions_per_patient).")
    hints = check_unbound_variables(parse(query))
    assert [hint.split(":")[0] for hint in hints] == ["AVG(?prescriptions_per_patient)", "MAX(?prescriptions_per_patient)"]
    assert all(hint.endswith(sketch) for hint in hints)
    stay = (f"SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) (AVG(?stay_days) AS ?avg_stay) WHERE {{ {SEX} }} GROUP BY ?sex")
    assert check_unbound_variables(parse(stay)) == [
        "AVG(?stay_days): nothing binds ?stay_days: add BIND(<expression> AS ?stay_days) or a pattern that binds it."
    ]


def test_count_of_an_unbound_variable_suggests_the_conditional_count():
    query = ("SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?cholera_prescription) AS ?cholera) "
             "WHERE { GRAPH ?g { ?p a sphn:DrugPrescription } }")
    assert check_unbound_variables(parse(query)) == [
        "COUNT(?cholera_prescription): nothing binds ?cholera_prescription. Count a subgroup with "
        "OPTIONAL { GRAPH ?g { <condition> } BIND(?g AS ?cholera_prescription) } and COUNT(DISTINCT ?cholera_prescription)."
    ]


def test_variables_read_only_by_bind_or_filter_expressions_are_not_reported():
    # official-04's reference leaves a column empty on purpose by passing an unbound variable to IF().
    query = ("SELECT (COUNT(DISTINCT ?g) AS ?n) (COUNT(DISTINCT ?born_abroad) AS ?m) WHERE { GRAPH ?g { ?b a sphn:Birth } "
             "FILTER(?never_bound_either > 1) "
             "BIND(IF(NOT EXISTS { GRAPH ?g { ?x a sphn:Nationality } }, ?g, ?never_bound) AS ?born_abroad) }")
    assert check_unbound_variables(parse(query)) == []


def test_graph_values_bind_sub_select_projections_and_aliases_bind():
    query = (f"SELECT ?sex ?label (COUNT(DISTINCT ?g) AS ?n) (SUM(?diagnoses) AS ?total) WHERE {{ {SEX} "
             f"{PER_PATIENT_DIAGNOSES} BIND(UCASE(?sex) AS ?label) }} GROUP BY ?sex ?label ORDER BY DESC(?n) ?total")
    assert check_unbound_variables(parse(query)) == []


def test_minus_and_exists_patterns_bind_nothing_outside():
    minus = "SELECT ?d WHERE { GRAPH ?g { ?x a sphn:Death } MINUS { GRAPH ?g { ?x sphn:hasDate ?d } } }"
    assert check_unbound_variables(parse(minus)) == [
        "?d is used in SELECT but nothing binds it: add BIND(<expression> AS ?d) or a pattern that binds it."
    ]
    exists = "SELECT ?d WHERE { GRAPH ?g { ?x a sphn:Death } FILTER EXISTS { GRAPH ?g { ?x sphn:hasDate ?d } } }"
    assert len(check_unbound_variables(parse(exists))) == 1


def test_select_star_and_other_query_forms_are_left_to_the_guard():
    assert check_unbound_variables(parse("SELECT * WHERE { GRAPH ?g { ?x a sphn:Death } FILTER(?zz > 1) }")) == []
    assert check_unbound_variables(parse("ASK { ?s ?p ?o }")) == []
    assert check_unbound_variables(parse("SELECT ?x WHERE { ?x ")) == []


# ---------------------------------------------------------------- re-bound variables

def test_bind_that_rebinds_an_earlier_bind_is_rejected():
    query = ("SELECT ?decade (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?e a sphn:AdverseEvent ; sphn:hasOnsetDateTime ?o } "
             "BIND(FLOOR(YEAR(?o) / 10) * 10 AS ?decade) BIND(IF(?decade = 0, 1970, ?decade) AS ?decade) } GROUP BY ?decade")
    assert check_rebound_variables(parse(query)) == [
        "BIND(... AS ?decade) re-binds ?decade, which is already bound in this scope: Virtuoso rejects it "
        "(SP031 Alias ?decade is defined twice). Bind the new value to a new variable name and use that name below, "
        "or merge both expressions into one."
    ]


def test_bind_that_rebinds_a_pattern_variable_or_a_sub_select_export_is_rejected():
    pattern = ("SELECT ?certainty (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?e a sphn:AllergyEpisode ; "
               "sphn:hasCertaintyCode ?cc . ?cc a ?certainty } "
               'BIND(IF(?certainty = snomed:410605003, "Confirmed", "Other") AS ?certainty) } GROUP BY ?certainty')
    assert [hint.split(" re-binds")[0] for hint in check_rebound_variables(parse(pattern))] == ["BIND(... AS ?certainty)"]
    exported = (f"SELECT (AVG(?diagnoses) AS ?avg) WHERE {{ {PER_PATIENT_DIAGNOSES} "
                "BIND(COALESCE(?diagnoses, 0) AS ?diagnoses) }")
    assert [hint.split(" re-binds")[0] for hint in check_rebound_variables(parse(exported))] == ["BIND(... AS ?diagnoses)"]
    inside_optional = ("SELECT ?group (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { OPTIONAL { ?follow_up a sphn:FollowUp ; "
                       "sphn:hasCode ?c . BIND(?g AS ?follow_up) } } } GROUP BY ?group")
    assert [hint.split(" re-binds")[0] for hint in check_rebound_variables(parse(inside_optional))] == ["BIND(... AS ?follow_up)"]


def test_select_expression_that_rebinds_a_where_variable_is_named_as_such():
    query = (f"SELECT (AVG(?diagnoses) AS ?diagnoses) WHERE {{ {PER_PATIENT_DIAGNOSES} }}")
    hints = check_rebound_variables(parse(query))
    assert len(hints) == 1 and hints[0].startswith("the SELECT expression (... AS ?diagnoses) re-binds ?diagnoses")


def test_legitimate_bindings_are_not_rebindings():
    projected_key = f"SELECT ?sex (COUNT(DISTINCT ?g) AS ?n) WHERE {{ {SEX} }} GROUP BY ?sex"
    assert check_rebound_variables(parse(projected_key)) == []
    union_branches = ("SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { { GRAPH ?g { ?a a sphn:Death } BIND(?g AS ?x) } "
                      "UNION { GRAPH ?g { ?b a sphn:Birth } BIND(?g AS ?x) } }")
    assert check_rebound_variables(parse(union_branches)) == []
    conditional_count = ("SELECT (COUNT(DISTINCT ?g) AS ?n) (COUNT(DISTINCT ?died) AS ?deaths) WHERE { GRAPH ?g { ?b a sphn:Birth } "
                         "OPTIONAL { GRAPH ?g { ?d a sphn:Death } BIND(?g AS ?died) } }")
    assert check_rebound_variables(parse(conditional_count)) == []
    inner_name_reused = ("SELECT (AVG(?dx) AS ?avg) WHERE { { SELECT ?g (COUNT(DISTINCT ?dx) AS ?diagnoses) "
                         "WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis } } GROUP BY ?g } BIND(?diagnoses * 2 AS ?dx) }")
    assert check_rebound_variables(parse(inner_name_reused)) == []
    official_alias = "SELECT (COUNT(?pid) AS ?n) WHERE { { SELECT DISTINCT(?g AS ?pid) WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis } } } }"
    assert check_rebound_variables(parse(official_alias)) == []


# ---------------------------------------------------------------- aggregate over aggregate

def test_nested_aggregate_is_rejected_with_a_sub_select_sketch():
    query = ("SELECT ?c (COUNT(DISTINCT ?g) AS ?n) (COUNT(DISTINCT ?g) / 1.0 / SUM(COUNT(DISTINCT ?g)) AS ?share) "
             "WHERE { GRAPH ?g { ?p a sphn:BilledProcedure ; sphn:hasIntent ?i . ?i a ?c } } GROUP BY ?c")
    assert check_aggregate_over_aggregate(parse(query)) == [
        "SUM(COUNT(DISTINCT ?g)) nests one aggregate inside another, which SPARQL does not allow (Virtuoso: SQ200 "
        "Aggregate function not allowed in context). Compute the inner aggregate in a sub-select, e.g. "
        "{ SELECT (COUNT(DISTINCT ?g) AS ?total) WHERE { ... } }, and use ?total in the outer SELECT."
    ]


def test_aggregate_over_an_alias_of_the_same_select_is_rejected():
    query = ("SELECT ?side (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?g) AS ?total_procedures) "
             "(ROUND(1.0 * COUNT(DISTINCT ?g) / COUNT(DISTINCT ?total_procedures)) AS ?share) "
             "WHERE { GRAPH ?g { ?p a sphn:BilledProcedure ; sphn:hasBodySite ?side } } GROUP BY ?side")
    assert check_aggregate_over_aggregate(parse(query)) == [
        "COUNT(DISTINCT ?total_procedures): ?total_procedures is an alias computed in this SELECT, and an aggregate "
        "cannot read another aggregate's result (Virtuoso: SQ200 Aggregate function not allowed in context). "
        "Aggregate over a variable bound in WHERE instead, or compute ?total_procedures in a sub-select first."
    ]
    # check_unbound_variables leaves the alias to this check: one hint, not two.
    assert check_unbound_variables(parse(query)) == []


def test_aliases_in_later_expressions_and_aggregated_group_keys_are_fine():
    later_expression = ("SELECT (COUNT(DISTINCT ?g) AS ?n) (SUM(?v) AS ?s) (ROUND(100 * ?s / ?n) AS ?pct) "
                        "WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasValue ?v } }")
    assert check_aggregate_over_aggregate(parse(later_expression)) == []
    group_key = f"SELECT ?sex (COUNT(DISTINCT ?sex) AS ?n) (SAMPLE(?g) AS ?one) WHERE {{ {SEX} }} GROUP BY ?sex"
    assert check_aggregate_over_aggregate(parse(group_key)) == []


# ---------------------------------------------------------------- FILTER on an averaged variable

def test_filter_on_an_averaged_variable_next_to_a_count_is_reported():
    query = ("SELECT (AVG(?diagnoses) AS ?avg) (MAX(?diagnoses) AS ?max) (COUNT(DISTINCT ?g) AS ?more_than_one) "
             f"WHERE {{ {PER_PATIENT_DIAGNOSES} FILTER(?diagnoses > 1) }}")
    assert check_filter_restricts_statistic(parse(query)) == [
        "FILTER(?diagnoses > 1) also restricts AVG(?diagnoses), MAX(?diagnoses): they are computed over the "
        "filtered rows only. Drop the FILTER and count the rows that satisfy it with SUM(IF(?diagnoses > 1, 1, 0))."
    ]
    mirrored = query.replace("FILTER(?diagnoses > 1)", "FILTER(1 < ?diagnoses && BOUND(?g))")
    assert check_filter_restricts_statistic(parse(mirrored))[0].startswith("FILTER(?diagnoses > 1) also restricts")


def test_filters_that_define_a_cohort_are_legitimate():
    without_count = "SELECT (AVG(?v) AS ?avg) WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasValue ?v } FILTER(?v > 18) }"
    assert check_filter_restricts_statistic(parse(without_count)) == []
    # "Patients over 65: how many, and their average age": a threshold on a raw value is a cohort, not a mistake.
    raw_value = ("SELECT (COUNT(DISTINCT ?g) AS ?n) (AVG(?v) AS ?avg) WHERE { GRAPH ?g { ?a a sphn:Age ; sphn:hasValue ?v } "
                 "FILTER(?v > 65) }")
    assert check_filter_restricts_statistic(parse(raw_value)) == []
    zero_threshold = (f"SELECT (AVG(?diagnoses) AS ?avg) (COUNT(DISTINCT ?g) AS ?n) WHERE {{ {PER_PATIENT_DIAGNOSES} "
                      "FILTER(?diagnoses > 0) FILTER(?diagnoses >= 1) }")
    assert check_filter_restricts_statistic(parse(zero_threshold)) == []
    inside_optional = ("SELECT (AVG(?v) AS ?avg) (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?a a sphn:Age } "
                       "OPTIONAL { GRAPH ?g { ?a sphn:hasValue ?v } FILTER(?v > 18) } }")
    assert check_filter_restricts_statistic(parse(inside_optional)) == []
    date_literal = ("SELECT (MIN(?d) AS ?first) (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?c a sphn:AdministrativeCase ; "
                    'sphn:hasAdmissionDateTime ?d } FILTER(?d >= "2015-01-01T00:00:00"^^xsd:dateTime) }')
    assert check_filter_restricts_statistic(parse(date_literal)) == []
    in_sub_select = ("SELECT (AVG(?diagnoses) AS ?avg) (COUNT(DISTINCT ?g) AS ?n) WHERE { { SELECT ?g (COUNT(DISTINCT ?dx) AS ?diagnoses) "
                     "WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasRank ?r } FILTER(?r > 1) } GROUP BY ?g } }")
    assert check_filter_restricts_statistic(parse(in_sub_select)) == []


# ---------------------------------------------------------------- two OPTIONAL blocks under AVG

def test_two_optional_blocks_feeding_averages_are_reported_with_the_union_rewrite():
    query = (f"SELECT ?sex (AVG(?bmi_value) AS ?avg_bmi) (AVG(?sbp_value) AS ?avg_sbp) (COUNT(DISTINCT ?g) AS ?n) WHERE {{ {SEX} "
             "OPTIONAL { GRAPH ?g { ?b a sphn:BodyMassIndex ; sphn:hasQuantity ?bq . ?bq sphn:hasValue ?bmi_value } } "
             "OPTIONAL { GRAPH ?g { ?p a sphn:BloodPressure ; sphn:hasSystolicPressure ?sq . ?sq sphn:hasValue ?sbp_value } } "
             "} GROUP BY ?sex")
    assert check_optional_cross_product(parse(query)) == [
        "AVG(?bmi_value) and AVG(?sbp_value) read variables bound in 2 different OPTIONAL blocks: the rows of one block "
        "are repeated for every row of the other, so averages, sums and counts are weighted wrongly. Put each block in "
        "its own UNION branch instead: { GRAPH ?g { ... ?bmi_value } BIND(?g AS ?bmi_value_patient) } UNION "
        "{ GRAPH ?g { ... ?sbp_value } BIND(?g AS ?sbp_value_patient) }, and count the patients of each branch with "
        "COUNT(DISTINCT ?bmi_value_patient)."
    ]
    rewritten = (f"SELECT ?sex (AVG(?bmi_value) AS ?avg_bmi) (AVG(?sbp_value) AS ?avg_sbp) WHERE {{ {SEX} "
                 "{ GRAPH ?g { ?b a sphn:BodyMassIndex ; sphn:hasQuantity ?bq . ?bq sphn:hasValue ?bmi_value } BIND(?g AS ?bmi_patient) } "
                 "UNION { GRAPH ?g { ?p a sphn:BloodPressure ; sphn:hasSystolicPressure ?sq . ?sq sphn:hasValue ?sbp_value } "
                 "BIND(?g AS ?sbp_patient) } } GROUP BY ?sex")
    assert check_optional_cross_product(parse(rewritten)) == []


def test_optional_blocks_under_min_max_or_distinct_counts_do_not_multiply():
    # official-01's reference takes MIN of two OPTIONAL dates: repeated rows cannot change a minimum.
    min_max = ("SELECT ?g (MIN(?a) AS ?first_surgery) (MAX(?b) AS ?last_rt) (COUNT(DISTINCT ?s) AS ?surgeries) "
               "WHERE { GRAPH ?g { ?d a sphn:OncologyDiagnosis } "
               "OPTIONAL { GRAPH ?g { ?s a sphn:OncologySurgery ; sphn:hasStartDateTime ?a } } "
               "OPTIONAL { GRAPH ?g { ?r a sphn:RadiotherapyProcedure ; sphn:hasStartDateTime ?b } } } GROUP BY ?g")
    assert check_optional_cross_product(parse(min_max)) == []
    one_block = (f"SELECT ?sex (AVG(?bmi_value) AS ?avg_bmi) (AVG(?age) AS ?avg_age) WHERE {{ {SEX} "
                 "GRAPH ?g { ?a a sphn:Age ; sphn:hasValue ?age } "
                 "OPTIONAL { GRAPH ?g { ?b a sphn:BodyMassIndex ; sphn:hasQuantity ?bq . ?bq sphn:hasValue ?bmi_value } } } GROUP BY ?sex")
    assert check_optional_cross_product(parse(one_block)) == []


# ---------------------------------------------------------------- false-positive corpora

@pytest.mark.parametrize("example", load_examples(), ids=lambda example: example["id"])
def test_no_check_flags_the_official_examples(example):
    parsed = parse_sparql(add_missing_prefixes(example["sparql"]))
    assert [check(parsed) for check in CHECKS] == [[] for _ in CHECKS]


@pytest.mark.parametrize("row", load_dev_references(), ids=lambda row: row["id"])
def test_no_check_flags_the_dev_reference_queries(row):
    parsed = parse_sparql(add_missing_prefixes(row["reference_sparql"]))
    assert parsed.kind == "SelectQuery"
    assert [check(parsed) for check in CHECKS] == [[] for _ in CHECKS]
