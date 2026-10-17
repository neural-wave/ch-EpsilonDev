import difflib
import json

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.context.schema import SchemaCards
from nl2sparql.judge import Judge
from nl2sparql.judge_checks import rewrites_aggregation
from nl2sparql.judge_checks.rewrites_aggregation import (
    ADDITIONS,
    REWRITES,
    _accepted,
    add_indicator_total,
    add_patient_count,
    add_patient_twins,
    add_share_parts,
    rewrite_dead_code_filter,
    rewrite_graph_count_named_as_records,
    rewrite_group_by_patient_rows,
    rewrite_group_key_bound_only_in_optional,
    rewrite_optional_bind_without_pattern,
    rewrite_scaled_statistic,
)
from nl2sparql.sparql.parse import ParsedQuery, parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes, fix_standard_prefixes

OPTION = 'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")'
SEX = (
    "GRAPH ?g { ?sex_record a sphn:AdministrativeSex ; sphn:hasCode ?sex_code . ?sex_code a ?sex_concept . }\n"
    '  VALUES (?sex_concept ?sex) { (snomed:248153007 "male") (snomed:248152002 "female") }\n'
)
PER_PATIENT_PRESCRIPTIONS = (
    "{ SELECT ?g (COUNT(DISTINCT ?prescription) AS ?prescriptions) WHERE { GRAPH ?g { "
    "?prescription a sphn:DrugPrescription . } } GROUP BY ?g }\n"
)
# The 17 October run's queries the rewrites were verified on (dev outcome in brackets).
Q062_FIRST_TRY = """
SELECT (COUNT(DISTINCT ?procedure) AS ?procedures) (COUNT(DISTINCT ?g) AS ?patients)
WHERE {
  GRAPH ?g {
    ?procedure a sphn:ImagingProcedure ;
               sphn:hasAdministrativeCase ?case ;
               sphn:hasStartDateTime ?start .
    ?case a sphn:AdministrativeCase ;
          sphn:hasAdmissionDateTime ?admission ;
          sphn:hasDischargeDateTime ?discharge .
  }
  FILTER (?start >= ?admission && ?start <= ?discharge)
}
GROUP BY ?g ?procedure
"""  # 101 rows -> exact
Q003_FIRST_TRY = """
SELECT (COUNT(DISTINCT ?g) AS ?stays)
       (ROUND(AVG(?days)) AS ?avg_days)
       (MIN(?days) AS ?min_days)
       (MAX(?days) AS ?max_days)
WHERE {
  {
    SELECT DISTINCT ?g ?admission ?discharge
    WHERE {
      GRAPH ?g {
        ?case a sphn:AdministrativeCase ;
              sphn:hasAdmissionDateTime ?admission ;
              sphn:hasDischargeDateTime ?discharge .
      }
    }
  }
  BIND (bif:datediff("day", ?admission, ?discharge) AS ?days)
}
GROUP BY ?g ?admission ?discharge
"""  # 2000 rows -> exact_numbers
Q042 = """
SELECT ?certainty (COUNT(DISTINCT ?g) AS ?episodes) (COUNT(DISTINCT ?patient) AS ?patients)
WHERE {
  GRAPH ?g {
    ?episode a sphn:AllergyEpisode ;
             sphn:hasCertaintyCode ?certainty_code .
    ?certainty_code a ?certainty .
  }
  OPTIONAL {
    GRAPH ?g {
      ?episode sphn:hasSubjectPseudoIdentifier ?pid .
      BIND(?g AS ?patient)
    }
  }
  FILTER (?certainty = snomed:410605003 || ?certainty = snomed:410592001 || ?certainty = snomed:415684004)
}
GROUP BY ?certainty
ORDER BY ?certainty
"""  # episodes = patients -> exact_numbers
Q050 = f"""
SELECT ?cholera_type
       (COUNT(DISTINCT ?g) AS ?patients)
       (COUNT(DISTINCT ?dx) AS ?diagnoses)
WHERE {{
  GRAPH ?g {{
    ?dx a sphn:BilledDiagnosis ;
        sphn:hasCode ?dx_code .
    ?dx_code a icd-10-gm:A00 {OPTION} .
  }}
  OPTIONAL {{
    GRAPH ?g {{ ?dx_code a icd-10-gm:A00.0 {OPTION} . }}
    BIND("classical" AS ?cholera_type)
  }}
  OPTIONAL {{
    GRAPH ?g {{ ?dx_code a icd-10-gm:A00.1 {OPTION} . }}
    BIND("el tor" AS ?cholera_type)
  }}
  OPTIONAL {{
    GRAPH ?g {{ ?dx_code a icd-10-gm:A00.9 {OPTION} . }}
    BIND("unspecified" AS ?cholera_type)
  }}
}}
GROUP BY ?cholera_type
"""  # a fourth row with an empty key -> exact_numbers
Q054 = f"""
SELECT ?year (COUNT(DISTINCT ?g) AS ?procedures) (COUNT(DISTINCT ?patient) AS ?patients)
WHERE {{
  GRAPH ?g {{
    ?proc a sphn:BilledProcedure ;
          sphn:hasCode ?code .
    ?code a chop:00.0 {OPTION} .
    ?code a ?concept .
    FILTER (STRSTARTS(STR(?concept), STR(chop:)))
    BIND (REPLACE(STR(?concept), "^.*/", "") AS ?chop_code)
    FILTER (REGEX(?chop_code, "^00\\\\.0[0-9]$"))
    ?proc sphn:hasSubjectPseudoIdentifier ?patient .
    ?proc sphn:hasStartDateTime ?start .
  }}
  BIND (YEAR(?start) AS ?year)
  FILTER (YEAR(?start) >= 2015 && YEAR(?start) <= 2024)
}}
GROUP BY ?year
ORDER BY ?year
"""  # procedures = patients, category code dropped -> exact
Q064 = f"""
SELECT ?sex
       (COUNT(DISTINCT ?g) AS ?patients)
       (ROUND(1000.0 * AVG(?prescriptions)) / 10 AS ?avg_prescriptions)
       (ROUND(1000.0 * MAX(?prescriptions)) / 10 AS ?max_prescriptions)
       (COUNT(DISTINCT ?many_prescriptions) AS ?patients_with_more_than_three)
       (ROUND(1000.0 * COUNT(DISTINCT ?many_prescriptions)) / COUNT(DISTINCT ?g) / 10 AS ?share_with_more_than_three)
WHERE {{
  {SEX}
  {PER_PATIENT_PRESCRIPTIONS}
  OPTIONAL {{
    BIND(IF(?prescriptions > 3, ?g, ?unbound) AS ?many_prescriptions)
  }}
}}
GROUP BY ?sex
ORDER BY ?sex
"""  # averages x100, nobody above three -> exact


def prepared(query: str) -> tuple[str, ParsedQuery]:
    """The text as the judge hands it to the rewrites: prefixes fixed and added, parsed."""
    text = add_missing_prefixes(fix_standard_prefixes(query.strip() + "\n"))
    parsed = parse_sparql(text)
    assert parsed.kind == "SelectQuery", parsed.error
    return text, parsed


def apply(rewrite, query: str) -> tuple[str, list[str]]:
    text, parsed = prepared(query)
    rewritten, repairs = rewrite(text, parsed)
    if repairs:
        assert rewritten != text
        assert parse_sparql(rewritten).kind == "SelectQuery"
    else:
        assert rewritten == text
    return rewritten, repairs


def apply_all(query: str) -> tuple[str, list[str]]:
    """The judge's loop: every rewrite in order, re-parsing after each applied one."""
    text, parsed = prepared(query)
    names = []
    for rewrite in REWRITES:
        rewritten, repairs = rewrite(text, parsed)
        if repairs:
            parsed = parse_sparql(rewritten)
            assert parsed.kind == "SelectQuery", (rewrite.__name__, parsed.error)
            text = rewritten
            names += [rewrite.__name__] * len(repairs)
    return text, names


def reference_queries() -> list[tuple[str, str]]:
    """The official examples, the team examples and the dev reference queries; holdout rows are a blind
    exam and stay closed."""
    queries = [(row["id"], row["sparql"]) for row in load_examples()]
    for line in (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            queries.append((row["id"], row["sparql"]))
    for line in (DATA_DIR.parent / "eval" / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("split") == "dev":
                queries.append((row["id"], row["reference_sparql"]))
    return queries


def test_rewrites_run_in_this_order():
    assert REWRITES == [
        rewrite_group_by_patient_rows, rewrite_graph_count_named_as_records, rewrite_group_key_bound_only_in_optional,
        rewrite_optional_bind_without_pattern, rewrite_scaled_statistic, rewrite_dead_code_filter,
    ]


@pytest.mark.parametrize("case_id,query", reference_queries(), ids=[case[0] for case in reference_queries()])
def test_no_rewrite_touches_a_reference_query(case_id, query):
    text = add_missing_prefixes(fix_standard_prefixes(query.strip() + "\n"))
    parsed = parse_sparql(text)
    if parsed.kind != "SelectQuery":  # official-14 is a CONSTRUCT, which the guard rejects
        return
    for rewrite in REWRITES:
        assert rewrite(text, parsed) == (text, []), (case_id, rewrite.__name__)


def test_every_rewrite_leaves_a_non_select_query_alone():
    text = "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nASK { GRAPH ?g { ?x a sphn:Birth } }\n"
    parsed = parse_sparql(text)
    assert parsed.kind == "AskQuery"
    for rewrite in REWRITES:
        assert rewrite(text, parsed) == (text, [])


def test_a_result_that_does_not_parse_or_projects_other_columns_is_refused():
    text, parsed = prepared(Q062_FIRST_TRY)
    assert _accepted(parsed, "SELECT ?x WHERE {") is None
    assert _accepted(parsed, text.replace("(COUNT(DISTINCT ?g) AS ?patients)", "")) is None
    assert _accepted(parsed, text.replace("GROUP BY ?g ?procedure\n", "")) is not None


# ------------------------------------------------------------- 1. GROUP BY per patient under an aggregate-only SELECT
def test_group_by_patient_and_record_under_an_aggregate_only_select_is_dropped():
    rewritten, repairs = apply(rewrite_group_by_patient_rows, Q062_FIRST_TRY)
    assert repairs == [
        "Dropped GROUP BY ?g ?procedure: every SELECT column is an aggregate, so the groups were unlabeled rows "
        "per patient; the query now returns one overall row"
    ]
    assert "GROUP BY" not in rewritten
    assert rewritten.rstrip().endswith("FILTER (?start >= ?admission && ?start <= ?discharge)\n}")


def test_group_by_patient_with_other_keys_is_dropped_too():
    rewritten, repairs = apply(rewrite_group_by_patient_rows, Q003_FIRST_TRY)
    assert [repair.split(":")[0] for repair in repairs] == ["Dropped GROUP BY ?g ?admission ?discharge"]
    assert "GROUP BY ?g ?admission" not in rewritten
    assert "SELECT DISTINCT ?g ?admission ?discharge" in rewritten  # the sub-select is untouched


@pytest.mark.parametrize("keys,pattern", [
    ("?patient", "BIND(?g AS ?patient)"),  # an alias of ?g
    ("?procedure", ""),  # a typed record only
], ids=["alias-of-g", "record-only"])
def test_group_by_an_alias_of_the_graph_or_by_records_only_is_dropped(keys, pattern):
    query = (f"SELECT (COUNT(DISTINCT ?procedure) AS ?n) WHERE {{ GRAPH ?g {{ ?procedure a sphn:ImagingProcedure }} "
             f"{pattern} }} GROUP BY {keys}")
    rewritten, repairs = apply(rewrite_group_by_patient_rows, query)
    assert len(repairs) == 1 and "GROUP BY" not in rewritten


@pytest.mark.parametrize("query", [
    "SELECT ?g (COUNT(?x) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Birth } } GROUP BY ?g",  # projects the key: the guard's case
    "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Birth ; sphn:hasCode ?c } } GROUP BY ?c",  # a category key
    "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Birth } } GROUP BY ?g HAVING (COUNT(?x) > 1)",
    "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Birth } } GROUP BY ?g ORDER BY ?g",
    "SELECT (AVG(?n) AS ?avg) WHERE { { SELECT (COUNT(?x) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Birth } } GROUP BY ?g } }",
], ids=["projected-key", "category-key", "having", "order-by-key", "sub-select"])
def test_group_by_stays_when_a_key_is_projected_or_the_grouping_is_not_per_patient(query):
    assert apply(rewrite_group_by_patient_rows, query)[1] == []


# ------------------------------------------------------------- 2. COUNT(DISTINCT ?g) under a record-named column
def test_count_of_graphs_named_after_records_counts_the_record():
    rewritten, repairs = apply(rewrite_graph_count_named_as_records, Q042)
    assert repairs == [
        "COUNT(DISTINCT ?g) AS ?episodes counted patients under a record name: it now counts the sphn:AllergyEpisode "
        "records, COUNT(DISTINCT ?episode)"
    ]
    assert "(COUNT(DISTINCT ?episode) AS ?episodes) (COUNT(DISTINCT ?patient) AS ?patients)" in rewritten


def test_the_card_picker_synonyms_name_the_record():
    query = ("SELECT (COUNT(DISTINCT ?g) AS ?stays) (AVG(?days) AS ?avg_days) WHERE { GRAPH ?g { "
             "?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?a ; sphn:hasDischargeDateTime ?d } "
             "BIND(bif:datediff('day', ?a, ?d) AS ?days) }")
    rewritten, repairs = apply(rewrite_graph_count_named_as_records, query)
    assert repairs == [
        "COUNT(DISTINCT ?g) AS ?stays counted patients under a record name: it now counts the "
        "sphn:AdministrativeCase records, COUNT(DISTINCT ?case)"
    ]
    assert "(COUNT(DISTINCT ?case) AS ?stays)" in rewritten


def test_a_plain_count_of_graphs_becomes_a_distinct_record_count():
    query = "SELECT (COUNT(?g) AS ?episodes) WHERE { GRAPH ?g { ?episode a sphn:AllergyEpisode } }"
    rewritten, repairs = apply(rewrite_graph_count_named_as_records, query)
    assert len(repairs) == 1 and "(COUNT(DISTINCT ?episode) AS ?episodes)" in rewritten


def test_an_alias_without_a_record_word_is_not_the_checks_case():
    query = "SELECT (COUNT(?g) AS ?births) WHERE { GRAPH ?g { ?birth a sphn:Birth } }"  # "birth" is no record word
    assert apply(rewrite_graph_count_named_as_records, query)[1] == []


@pytest.mark.parametrize("query", [
    # two typed records share a stem with the alias: the hint says which, the rewrite would guess
    "SELECT (COUNT(DISTINCT ?g) AS ?events) WHERE { GRAPH ?g { ?a a sphn:AdverseEvent . ?l a sphn:LabTestEvent } }",
    # no stem in common
    "SELECT (COUNT(DISTINCT ?g) AS ?events) WHERE { GRAPH ?g { ?episode a sphn:AllergyEpisode } }",
    # the record is typed inside a sub-select that does not export it
    Q003_FIRST_TRY,
    # a patient word in the alias: patients are what is counted
    "SELECT (COUNT(DISTINCT ?g) AS ?patients_with_episodes) WHERE { GRAPH ?g { ?episode a sphn:AllergyEpisode } }",
    # the same column twice in two sub-selects: the text match is not unique
    ("SELECT (SUM(?episodes) AS ?total) WHERE { { SELECT (COUNT(DISTINCT ?g) AS ?episodes) WHERE { GRAPH ?g { "
     "?episode a sphn:AllergyEpisode } } } UNION { SELECT (COUNT(DISTINCT ?g) AS ?episodes) WHERE { GRAPH ?g { "
     "?episode a sphn:AllergyEpisode } } } }"),
], ids=["two-candidates", "no-stem", "out-of-scope", "patient-word", "ambiguous-text"])
def test_the_graph_count_stays_when_no_single_record_matches_the_alias(query):
    assert apply(rewrite_graph_count_named_as_records, query)[1] == []


# ------------------------------------------------------------ 3. GROUP BY key bound only by labels inside OPTIONAL
def test_group_key_bound_only_by_optional_labels_gets_filter_bound():
    rewritten, repairs = apply(rewrite_group_key_bound_only_in_optional, Q050)
    assert repairs == [
        "Added FILTER (BOUND(?cholera_type)): the GROUP BY key is bound only inside OPTIONAL blocks, so the rows "
        "that matched none of them made an extra group with an empty key"
    ]
    assert rewritten.rstrip().endswith(
        '    BIND("unspecified" AS ?cholera_type)\n  }\n  FILTER (BOUND(?cholera_type))\n}\nGROUP BY ?cholera_type'
    )


def test_filter_bound_goes_before_a_closing_brace_that_shares_its_line():
    query = ("SELECT ?label (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Birth } "
             "OPTIONAL { GRAPH ?g { ?x sphn:hasCode ?c } BIND('coded' AS ?label) } } GROUP BY ?label")
    rewritten, repairs = apply(rewrite_group_key_bound_only_in_optional, query)
    assert len(repairs) == 1
    assert "BIND('coded' AS ?label) }\n  FILTER (BOUND(?label))\n} GROUP BY ?label" in rewritten


@pytest.mark.parametrize("pattern", [
    "VALUES ?label { 'a' 'b' }",  # bound on every row
    "BIND('all' AS ?label)",  # a BIND outside OPTIONAL
    "OPTIONAL { GRAPH ?g { ?x sphn:hasCode ?c } BIND(?g AS ?label) }",  # a patient flag, not a label
    "OPTIONAL { GRAPH ?g { ?x sphn:hasCode ?c } BIND('coded' AS ?label) } BIND(COALESCE(?label, 'other') AS ?shown)",
    "OPTIONAL { GRAPH ?g { ?x sphn:hasCode ?c } BIND('coded' AS ?label) } FILTER (BOUND(?label))",  # already there
    "{ SELECT ?g ?label WHERE { GRAPH ?g { ?y a sphn:Birth } OPTIONAL { GRAPH ?g { ?y sphn:hasCode ?c } BIND('coded' AS ?label) } } }",
], ids=["values", "bind-outside", "graph-alias", "coalesce", "already-filtered", "sub-select"])
def test_keys_bound_elsewhere_or_handled_on_purpose_keep_their_empty_group(pattern):
    query = f"SELECT ?label (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?x a sphn:Birth }} {pattern} }} GROUP BY ?label"
    assert apply(rewrite_group_key_bound_only_in_optional, query)[1] == []


# ------------------------------------------------------------------------------ 4. OPTIONAL { BIND(...) } without a pattern
def test_bind_alone_in_an_optional_moves_out():
    rewritten, repairs = apply(rewrite_optional_bind_without_pattern, Q064)
    assert repairs == [
        "OPTIONAL { BIND(IF(?prescriptions > 3, ?g, ?unbound) AS ?many_prescriptions) } has no pattern, so the BIND "
        "could not see the variables bound outside the block: the BIND now stands on its own"
    ]
    assert "OPTIONAL" not in rewritten
    assert "  BIND(IF(?prescriptions > 3, ?g, ?unbound) AS ?many_prescriptions)\n}\nGROUP BY ?sex" in rewritten


def test_two_binds_in_one_empty_optional_both_move_out():
    query = (f"SELECT ?sex (SUM(?more) AS ?n) WHERE {{ {SEX} {PER_PATIENT_PRESCRIPTIONS} "
             "OPTIONAL { BIND(IF(?prescriptions > 3, 1, 0) AS ?more) . BIND(IF(?prescriptions > 5, 1, 0) AS ?many) } "
             "} GROUP BY ?sex")
    rewritten, repairs = apply(rewrite_optional_bind_without_pattern, query)
    assert len(repairs) == 1 and "OPTIONAL" not in rewritten
    assert "BIND(IF(?prescriptions > 3, 1, 0) AS ?more) . BIND(IF(?prescriptions > 5, 1, 0) AS ?many) }" in rewritten


@pytest.mark.parametrize("block", [
    "OPTIONAL { GRAPH ?g { ?rx a sphn:DrugPrescription } BIND(?g AS ?with_prescription) }",  # a pattern: a real OPTIONAL
    "OPTIONAL { BIND(IF(?prescriptions > 3, ?g, ?unbound) AS ?many) FILTER (BOUND(?many)) }",  # a filter rides along
], ids=["with-pattern", "with-filter"])
def test_an_optional_with_a_pattern_or_a_filter_stays(block):
    query = f"SELECT ?sex (COUNT(DISTINCT ?g) AS ?n) WHERE {{ {SEX} {PER_PATIENT_PRESCRIPTIONS} {block} }} GROUP BY ?sex"
    assert apply(rewrite_optional_bind_without_pattern, query)[1] == []


# ------------------------------------------------------------------------ 5. percentage scaling on a plain statistic
def test_percent_scaling_on_an_average_and_a_maximum_is_undone():
    rewritten, repairs = apply(rewrite_scaled_statistic, Q064)
    assert repairs == [
        "ROUND(1000.0 * AVG(?prescriptions)) / 10 is 100 times the average: written as ROUND(10 * AVG(?prescriptions)) / 10",
        "ROUND(1000.0 * MAX(?prescriptions)) / 10 is 100 times the maximum: written as MAX(?prescriptions)",
    ]
    assert "(ROUND(10 * AVG(?prescriptions)) / 10 AS ?avg_prescriptions)" in rewritten
    assert "(MAX(?prescriptions) AS ?max_prescriptions)" in rewritten
    assert "ROUND(1000.0 * COUNT(DISTINCT ?many_prescriptions)) / COUNT(DISTINCT ?g) / 10" in rewritten  # the share stays


def test_a_minimum_scaled_a_thousandfold_is_undone():
    query = f"SELECT ?sex (ROUND(10000 * MIN(?prescriptions)) / 10 AS ?min_rx) WHERE {{ {SEX} {PER_PATIENT_PRESCRIPTIONS} }} GROUP BY ?sex"
    rewritten, repairs = apply(rewrite_scaled_statistic, query)
    assert repairs == ["ROUND(10000 * MIN(?prescriptions)) / 10 is 1000 times the minimum: written as MIN(?prescriptions)"]
    assert "(MIN(?prescriptions) AS ?min_rx)" in rewritten


@pytest.mark.parametrize("column,pattern", [
    ("(ROUND(100.0 * AVG(?prescriptions)) / 100 AS ?avg_rx)", ""),  # q-035: the factor and the divisor cancel
    ("(ROUND(1000.0 * AVG(?flag)) / 10 AS ?pct_many)", "BIND(IF(?prescriptions > 3, 1, 0) AS ?flag)"),  # a 0/1 indicator
    ("(ROUND(1000.0 * AVG(?flag)) / 10 AS ?avg_flag)", "BIND(IF(?prescriptions > 3, 1.0, 0.0) AS ?flag)"),
    ("(ROUND(1000.0 * AVG(?prescriptions)) / 10 AS ?share_rx)", ""),  # the alias names a share
    ("(ROUND(1000.0 * AVG(?prescriptions) / COUNT(DISTINCT ?g)) / 10 AS ?x)", ""),  # a ratio
    ("(ROUND(1000.0 * AVG(?prescriptions)) / 10 / COUNT(DISTINCT ?g) AS ?x)", ""),  # divided again
], ids=["cancelling", "indicator", "float-indicator", "share-alias", "ratio", "divided-again"])
def test_scaling_stays_when_it_is_a_share_or_cancels_out(column, pattern):
    query = f"SELECT ?sex {column} WHERE {{ {SEX} {PER_PATIENT_PRESCRIPTIONS} {pattern} }} GROUP BY ?sex"
    assert apply(rewrite_scaled_statistic, query)[1] == []


def test_an_indicator_summed_in_a_sub_select_is_a_share():
    query = ("SELECT ?sex (ROUND(1000.0 * AVG(?has_many)) / 10 AS ?avg_many) WHERE { " + SEX
             + "{ SELECT ?g (MAX(IF(?n > 3, 1, 0)) AS ?has_many) WHERE { { SELECT ?g (COUNT(?rx) AS ?n) WHERE { GRAPH ?g { "
               "?rx a sphn:DrugPrescription } } GROUP BY ?g } } GROUP BY ?g } } GROUP BY ?sex")
    assert apply(rewrite_scaled_statistic, query)[1] == []


# --------------------------------------------------------------------------- 6. dead REGEX filter on a code's concept
def test_subcode_regex_copied_from_an_example_goes_with_its_chain():
    rewritten, repairs = apply(rewrite_dead_code_filter, Q054)
    assert repairs == [
        "Removed ?code a ?concept with the BIND and FILTER(REGEX) on it: ?code is already typed chop:00.0, ?concept "
        "is never returned, and the regex copied from an example dropped the codes outside its pattern"
    ]
    assert rewritten == prepared(Q054)[0].replace(
        "    ?code a ?concept .\n"
        "    FILTER (STRSTARTS(STR(?concept), STR(chop:)))\n"
        '    BIND (REPLACE(STR(?concept), "^.*/", "") AS ?chop_code)\n'
        '    FILTER (REGEX(?chop_code, "^00\\\\.0[0-9]$"))\n', ""
    )


def test_a_regex_on_the_full_iri_is_dead_code_too():
    query = (f"SELECT (COUNT(DISTINCT ?proc) AS ?procedures) WHERE {{ GRAPH ?g {{ ?proc a sphn:BilledProcedure ; "
             f"sphn:hasCode ?code . ?code a chop:00.0 {OPTION} . ?code a ?concept . "
             'FILTER (REGEX(STR(?concept), "/00\\\\.0[0-9]$")) } }')
    rewritten, repairs = apply(rewrite_dead_code_filter, query)
    assert len(repairs) == 1
    assert "?concept" not in rewritten and f"?code a chop:00.0 {OPTION} ." in rewritten


CHAIN = (f"?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a chop:00.0 {OPTION} . ?code a ?concept . "
         'BIND (REPLACE(STR(?concept), "^.*/", "") AS ?chop_code) ')
SUBCODE_REGEX = 'FILTER (REGEX(?chop_code, "^00\\\\.0[0-9]$"))'


@pytest.mark.parametrize("select,filters,clause", [
    ("?chop_code (COUNT(DISTINCT ?proc) AS ?procedures)", SUBCODE_REGEX, "GROUP BY ?chop_code"),  # the id is returned (q-055)
    ("(COUNT(DISTINCT ?proc) AS ?procedures) (SAMPLE(?label) AS ?l)", f"{SUBCODE_REGEX} ?concept rdfs:label ?label .", ""),
    ("(COUNT(DISTINCT ?proc) AS ?procedures)", f"{SUBCODE_REGEX} FILTER (?concept != chop:00.01)", ""),  # a real restriction
    ("(COUNT(DISTINCT ?proc) AS ?procedures)", 'FILTER (REGEX(?chop_code, "^00\\\\.01"))', ""),  # a subcode, not the code
    ("(COUNT(DISTINCT ?proc) AS ?procedures)", 'FILTER (REGEX(?chop_code, "^00\\\\.0[0-9]$") && ?proc != ?code)', ""),
], ids=["grouped", "label", "exclusion", "subcode-regex", "regex-with-other-variable"])
def test_the_chain_stays_when_the_concept_is_used_or_the_regex_restricts(select, filters, clause):
    query = f"SELECT {select} WHERE {{ GRAPH ?g {{ {CHAIN} {filters} }} }} {clause}"
    assert apply(rewrite_dead_code_filter, query)[1] == []


def test_without_a_concrete_code_the_regex_is_the_restriction():
    query = ("SELECT (COUNT(DISTINCT ?proc) AS ?procedures) WHERE { GRAPH ?g { ?proc a sphn:BilledProcedure ; "
             "sphn:hasCode ?code . ?code a ?concept . BIND (REPLACE(STR(?concept), \"^.*/\", \"\") AS ?chop_code) "
             'FILTER (REGEX(?chop_code, "^00\\\\.0[0-9]$")) } }')
    assert apply(rewrite_dead_code_filter, query)[1] == []


def test_a_namespace_filter_alone_is_not_a_copied_regex():
    query = (f"SELECT (COUNT(DISTINCT ?proc) AS ?procedures) WHERE {{ GRAPH ?g {{ ?proc a sphn:BilledProcedure ; "
             f"sphn:hasCode ?code . ?code a chop:00.0 {OPTION} . ?code a ?concept . "
             "FILTER (STRSTARTS(STR(?concept), STR(chop:))) } }")
    assert apply(rewrite_dead_code_filter, query)[1] == []


# ----------------------------------------------------------------------------------- the chain, as the judge runs it
def test_q054_gets_the_record_count_and_loses_the_copied_regex():
    rewritten, names = apply_all(Q054)
    assert names == ["rewrite_graph_count_named_as_records", "rewrite_dead_code_filter"]
    assert "SELECT ?year (COUNT(DISTINCT ?proc) AS ?procedures) (COUNT(DISTINCT ?patient) AS ?patients)" in rewritten
    assert "?concept" not in rewritten and "REGEX" not in rewritten


def test_q064_gets_its_bind_out_of_the_optional_and_its_statistics_unscaled():
    rewritten, names = apply_all(Q064)
    assert names == ["rewrite_optional_bind_without_pattern", "rewrite_scaled_statistic", "rewrite_scaled_statistic"]
    assert "OPTIONAL" not in rewritten
    assert "(ROUND(10 * AVG(?prescriptions)) / 10 AS ?avg_prescriptions)" in rewritten
    assert "(MAX(?prescriptions) AS ?max_prescriptions)" in rewritten


def test_q003_loses_its_group_by_and_keeps_counting_graphs():
    rewritten, names = apply_all(Q003_FIRST_TRY)
    assert names == ["rewrite_group_by_patient_rows"]  # ?case is hidden in the sub-select: no record to count
    assert "(COUNT(DISTINCT ?g) AS ?stays)" in rewritten and "GROUP BY" not in rewritten


# ------------------------------------------------------- 7. the counts the house style always shows (additions)
# The 17 October answers the additions were verified on (dev outcome before -> after, in brackets).
Q035_SAVED = """
SELECT
       (ROUND(10 * AVG(?diagnoses)) / 10 AS ?avg_diagnoses_per_patient)
       (MAX(?diagnoses) AS ?max_diagnoses_per_patient)
       (SUM(IF(?diagnoses > 1, 1, 0)) AS ?patients_with_more_than_one)
WHERE {
  {
    SELECT ?g (COUNT(DISTINCT ?diagnosis) AS ?diagnoses)
    WHERE {
      GRAPH ?g { ?diagnosis a sphn:BilledDiagnosis . }
    }
    GROUP BY ?g
  }
}
"""  # same_rows -> exact
Q022_SAVED = """
SELECT ?care_type
       (ROUND(1000.0 * COUNT(DISTINCT ?case_with_proc) / COUNT(DISTINCT ?case)) / 10 AS ?share_with_procedure)
WHERE {
  GRAPH ?g {
    ?case a sphn:AdministrativeCase ;
          sphn:hasCareHandling ?handling .
    ?handling sphn:hasTypeCode ?code .
    ?code a ?concept .
  }
  FILTER (?concept IN (snomed:371883000, snomed:394656005, snomed:304903009))
  BIND (IF(?concept = snomed:371883000, "Outpatient",
           IF(?concept = snomed:394656005, "Inpatient", "Day-care")) AS ?care_type)
  OPTIONAL {
    GRAPH ?g {
      ?proc a sphn:BilledProcedure ;
            sphn:hasAdministrativeCase ?case .
    }
    BIND (?case AS ?case_with_proc)
  }
}
GROUP BY ?care_type
"""  # same_rows -> exact_numbers
Q074_SAVED = """
SELECT ?year
       (COUNT(DISTINCT ?imaging) AS ?procedures)
       (COUNT(DISTINCT ?g) AS ?patients)
       (ROUND(1000.0 * SUM(?is_guidance) / COUNT(?imaging)) / 10 AS ?guidance_share_pct)
WHERE {
  GRAPH ?g {
    ?imaging a sphn:ImagingProcedure ;
             sphn:hasStartDateTime ?start ;
             sphn:hasIntent ?intent_record .
    ?intent_record sphn:hasCode ?intent_code .
    ?intent_code a ?intent_concept .
  }
  BIND (YEAR(?start) AS ?year)
  BIND (IF(?intent_concept = snomed:429892002, 1, 0) AS ?is_guidance)
}
GROUP BY ?year
"""  # same_rows -> exact_numbers
Q050_SPLIT = f"""
SELECT (COUNT(DISTINCT ?g_classical) AS ?patients_classical)
       (COUNT(DISTINCT ?dx_classical) AS ?diagnoses_classical)
       (COUNT(DISTINCT ?g_eltor) AS ?patients_eltor)
WHERE {{
  OPTIONAL {{
    GRAPH ?g_classical {{
      ?dx_classical a sphn:BilledDiagnosis ; sphn:hasCode ?code_classical .
      ?code_classical a icd-10-gm:A00.0 {OPTION} .
    }}
  }}
  OPTIONAL {{
    GRAPH ?g_eltor {{
      ?dx_eltor a sphn:BilledDiagnosis ; sphn:hasCode ?code_eltor .
      ?code_eltor a icd-10-gm:A00.1 {OPTION} .
    }}
  }}
}}
"""  # every graph is conditional: no total to add
OFFICIAL_06_PRINCIPAL = f"""
SELECT ?concept
       (COUNT(DISTINCT ?procedure) AS ?count)
       (COUNT(DISTINCT ?principal) AS ?principal_procedures)
WHERE {{
  GRAPH ?g {{
    ?code a chop:00.0 {OPTION} .
    ?procedure sphn:hasCode ?code ;
               a sphn:BilledProcedure .
    ?code a ?concept .
    OPTIONAL {{
      ?procedure sphn:hasRank sphn-individual:Principal .
      BIND (?procedure AS ?principal)
    }}
  }}
}}
GROUP BY ?concept
"""
OFFICIAL_07_SAVED = """
SELECT ?route
       (COUNT(DISTINCT ?g) AS ?patients)
       (SUM(?in_hospital) AS ?in_hospital_count)
WHERE {
  GRAPH ?g {
    ?admin a sphn:DrugAdministrationEvent ;
           sphn:hasAdministrationRouteCode ?route_code ;
           sphn:hasStartDateTime ?start ;
           sphn:hasAdministrativeCase ?case .
    ?route_code a ?route .
    ?case sphn:hasAdmissionDateTime ?admission ;
          sphn:hasDischargeDateTime ?discharge .
  }
  BIND(IF(?start >= ?admission && ?start <= ?discharge, 1, 0) AS ?in_hospital)
}
GROUP BY ?route
"""
# The references the additions change, each still exact_numbers against its own rows on dev (17 October).
CHANGED_REFERENCES = {
    "official-02", "official-07", "official-10", "q-003", "q-022", "q-029", "q-052", "q-059", "q-074", "q-076",
}


def add_all(query: str) -> tuple[str, list[str]]:
    """The judge's last step: every addition in order, re-parsing after each applied one."""
    text, parsed = prepared(query)
    names = []
    for addition in ADDITIONS:
        rewritten, repairs = addition(text, parsed)
        if repairs:
            parsed = parse_sparql(rewritten)
            assert parsed.kind == "SelectQuery", (addition.__name__, parsed.error)
            text = rewritten
            names += [addition.__name__] * len(repairs)
    return text, names


def only_inserts(original: str, rewritten: str) -> bool:
    """The rewrite only inserts text: no character of the original is changed or removed."""
    opcodes = difflib.SequenceMatcher(None, original, rewritten, autojunk=False).get_opcodes()
    return all(tag in ("equal", "insert") for tag, *_ in opcodes)


def test_additions_run_in_this_order():
    assert ADDITIONS == [add_patient_count, add_share_parts, add_patient_twins, add_indicator_total]


@pytest.mark.parametrize("case_id,query", reference_queries(), ids=[case[0] for case in reference_queries()])
def test_additions_only_append_columns_to_the_references_measured_on_dev(case_id, query, guard):
    text = add_missing_prefixes(fix_standard_prefixes(query.strip() + "\n"))
    if parse_sparql(text).kind != "SelectQuery":
        return
    rewritten, names = add_all(query)
    assert bool(names) == (case_id in CHANGED_REFERENCES), names
    assert only_inserts(text, rewritten)
    assert add_all(rewritten) == (rewritten, [])  # a second pass adds nothing
    assert [v.rule for v in guard.check_query(rewritten)] == [v.rule for v in guard.check_query(text)]


def test_patient_count_is_added_when_a_sub_select_projects_the_graph():
    rewritten, repairs = apply(add_patient_count, Q035_SAVED)
    assert repairs == [
        "Added (COUNT(DISTINCT ?g) AS ?patients): the patients of every row, which the reference queries always show"
    ]
    assert ("       (SUM(IF(?diagnoses > 1, 1, 0)) AS ?patients_with_more_than_one)\n"
            "       (COUNT(DISTINCT ?g) AS ?patients)\nWHERE {") in rewritten
    assert add_all(Q035_SAVED)[1] == ["add_patient_count"]  # rows from a sub-select: no indicator total


def test_the_graph_variable_comes_from_the_graph_pattern_not_its_name():
    query = "SELECT (COUNT(DISTINCT ?dx) AS ?diagnoses) WHERE { GRAPH ?patient { ?dx a sphn:BilledDiagnosis . } }"
    rewritten, _ = apply(add_patient_count, query)
    assert "SELECT (COUNT(DISTINCT ?dx) AS ?diagnoses) (COUNT(DISTINCT ?patient) AS ?patients) WHERE" in rewritten
    assert apply(add_patient_count, Q050_SPLIT)[1] == []  # ?g_classical and ?g_eltor are bound only in OPTIONAL
    assert add_all(Q050_SPLIT)[1] == []


@pytest.mark.parametrize("query", [
    "SELECT (COUNT(DISTINCT ?dx) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } } GROUP BY ?g",
    "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } BIND (?g AS ?p) }",
    "SELECT (COUNT(DISTINCT ?a) AS ?n) WHERE { GRAPH ?g { ?a a sphn:Birth . } GRAPH ?h { ?b a sphn:Death . } }",
    "SELECT ?dx WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }",
    "SELECT (COUNT(*) AS ?patients) (AVG(?n) AS ?avg) WHERE { { SELECT ?g (COUNT(?dx) AS ?n) WHERE { "
    "GRAPH ?g { ?dx a sphn:BilledDiagnosis . } } GROUP BY ?g } }",
])
def test_no_patient_count_for_one_patient_per_group_a_copy_two_graphs_rows_or_patient_rows_counted(query):
    assert apply(add_patient_count, query)[1] == []


def test_the_parts_of_a_share_become_columns_and_the_conditional_count_gets_its_patients():
    rewritten, names = add_all(Q022_SAVED)
    assert names == ["add_patient_count", "add_share_parts", "add_share_parts", "add_patient_twins"]
    assert (
        "       (ROUND(1000.0 * COUNT(DISTINCT ?case_with_proc) / COUNT(DISTINCT ?case)) / 10 AS ?share_with_procedure)\n"
        "       (COUNT(DISTINCT ?g) AS ?patients)\n"
        "       (COUNT(DISTINCT ?case_with_proc) AS ?share_with_procedure_numerator)\n"
        "       (COUNT(DISTINCT ?case) AS ?share_with_procedure_denominator)\n"
        "       (COUNT(DISTINCT ?case_with_proc_patient) AS ?case_with_proc_patients)\nWHERE {"
    ) in rewritten
    assert "    BIND (?case AS ?case_with_proc)\n    BIND (?g AS ?case_with_proc_patient)\n  }" in rewritten
    assert add_all(rewritten) == (rewritten, [])


def test_a_summed_indicator_divided_by_a_count_gives_both_parts():
    rewritten, repairs = apply(add_share_parts, Q074_SAVED)
    assert repairs == [
        "Added (SUM(?is_guidance) AS ?guidance_share_pct_numerator): a part of a share, which the reference "
        "queries show next to it",
        "Added (COUNT(?imaging) AS ?guidance_share_pct_denominator): a part of a share, which the reference "
        "queries show next to it",
    ]
    assert apply(add_indicator_total, rewritten)[1] == []  # the SUM is part of a share: it has its denominator


@pytest.mark.parametrize("column", [
    '(IF(SUM(?treated) > 0, ROUND(1.0 * SUM(?days) / SUM(?treated)), "") AS ?avg_days)',  # an average (official-01)
    "(ROUND(1000.0 * COUNT(DISTINCT ?x) / COUNT(DISTINCT ?g)) / 10 AS ?pct)",  # both parts are columns already
    "(ROUND(10 * AVG(?days)) / 10 AS ?avg_days)",  # divided by a constant
])
def test_no_share_parts_for_an_average_parts_already_shown_or_a_constant_divisor(column):
    query = (f"SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?x) AS ?with_x) {column} WHERE {{ GRAPH ?g {{ "
             "?x a sphn:Birth ; sphn:hasRecordDateTime ?d . } BIND (1 AS ?treated) BIND (2 AS ?days) }")
    assert apply(add_share_parts, query)[1] == []


def test_a_conditional_record_count_inside_the_graph_block_gets_its_patients():
    rewritten, repairs = apply(add_patient_twins, OFFICIAL_06_PRINCIPAL)
    assert repairs == [
        "Added (COUNT(DISTINCT ?principal_patient) AS ?principal_patients) with BIND (?g AS ?principal_patient): the "
        "patients next to the conditional count of ?principal, which the reference queries show"
    ]
    assert "      BIND (?procedure AS ?principal)\n      BIND (?g AS ?principal_patient)\n    }" in rewritten
    assert "       (COUNT(DISTINCT ?principal_patient) AS ?principal_patients)\nWHERE {" in rewritten


@pytest.mark.parametrize("block", [
    "OPTIONAL { GRAPH ?g { ?dx a sphn:BilledDiagnosis . } BIND (?dx AS ?x) BIND (?g AS ?x_patient) }",  # twinned
    "OPTIONAL { BIND (?dx AS ?x) GRAPH ?g { ?dx a sphn:BilledDiagnosis . } }",  # ?g is bound after the BIND
    "GRAPH ?g { ?dx a sphn:BilledDiagnosis . } BIND (?dx AS ?x)",  # not conditional
])
def test_no_twin_when_the_block_has_its_patients_binds_late_or_is_not_optional(block):
    query = (f"SELECT (COUNT(DISTINCT ?x) AS ?diagnoses) (COUNT(DISTINCT ?x_patient) AS ?x_patients) "
             f"WHERE {{ GRAPH ?g {{ ?b a sphn:Birth . }} {block} }}")
    assert apply(add_patient_twins, query)[1] == []


def test_a_summed_indicator_without_a_total_gets_its_records():
    rewritten, names = add_all(OFFICIAL_07_SAVED)
    assert names == ["add_indicator_total"]  # ?case is untyped: the indicator is about the administrations
    assert ("       (SUM(?in_hospital) AS ?in_hospital_count)\n"
            "       (COUNT(DISTINCT ?admin) AS ?drug_administration_events)\nWHERE {") in rewritten
    counted = OFFICIAL_07_SAVED.replace("(COUNT(DISTINCT ?g) AS ?patients)", "(COUNT(?admin) AS ?administrations)")
    assert apply(add_indicator_total, counted)[1] == []


def test_the_judge_adds_the_columns_last_with_the_same_errors(monkeypatch):
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    question = "How many billed diagnoses does a patient have on average and at most, and how many have more than one?"
    result = judge.check(Q035_SAVED, question)
    assert "(COUNT(DISTINCT ?g) AS ?patients)" in result.sparql
    assert result.repairs[-1].startswith("Added (COUNT(DISTINCT ?g) AS ?patients)")
    monkeypatch.setattr(rewrites_aggregation, "ADDITIONS", [])
    without = judge.check(Q035_SAVED, question)
    assert "?patients)" not in without.sparql and without.errors == result.errors
