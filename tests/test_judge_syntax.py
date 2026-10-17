import json
import re
from pathlib import Path

import pytest

from conftest import DATA_DIR, PREFIXES, load_examples
from nl2sparql.context.schema import SchemaCards
from nl2sparql.judge_checks.syntax import HINTS, diagnose, normalize
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes, fix_standard_prefixes

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
SNOMED = "PREFIX snomed: <http://snomed.info/id/>\n"
BIF = "PREFIX bif: <bif:>\n"
INFERENCE = ' OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")'
DATA_TERMS = SchemaCards.load(DATA_DIR / "schema_cards.json").data_terms


def hints_for(query: str) -> list[str]:
    assert parse_sparql(query).kind == "Invalid"
    return diagnose(query)


def repaired(query: str, data_terms=None) -> tuple[str, list[str]]:
    assert parse_sparql(query).kind == "Invalid"
    fixed, applied = normalize(query, data_terms)
    assert parse_sparql(fixed).kind == "SelectQuery", fixed
    return fixed, applied


def load_team_examples() -> list[dict]:
    lines = (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def operator_chain(algebra) -> list[str]:
    names = []
    while hasattr(algebra, "name"):
        names.append(algebra.name)
        algebra = algebra.get("p")
    return names


def dev_reference_queries() -> list[tuple[str, str]]:
    # The holdout rows are a blind exam: their lines are skipped before being parsed.
    queries = []
    for line in (EVAL_DIR / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip() and re.search(r'"split"\s*:\s*"dev"', line):
            row = json.loads(line)
            queries.append((row["id"], row["reference_sparql"]))
    return queries


VALID_QUERIES = ([(example["id"], example["sparql"]) for example in load_examples() + load_team_examples()]
                 + dev_reference_queries())
assert len(VALID_QUERIES) == 64  # 16 official + 3 team examples + 45 dev references


# q-008, first try: class alternatives with "|" and GROUP BY true
Q008_FIRST = PREFIXES + SNOMED + """SELECT (COUNT(DISTINCT ?g) AS ?patients)
WHERE {
  GRAPH ?g {
    ?transplant a sphn:Transplant ; sphn:hasGraftTypeCode ?code .
    ?code a snomed:15879007 | snomed:7970006 .
  }
}
GROUP BY true
"""

# q-008, final answer: the second UNION branch written after the WHERE block
Q008_FINAL = PREFIXES + SNOMED + """SELECT (COUNT(DISTINCT ?g) AS ?autograft_patients)
WHERE {
  GRAPH ?g {
    ?transplant a sphn:Transplant ; sphn:hasGraftTypeCode ?code .
    ?code a snomed:15879007 .
  }
}
UNION
{
  GRAPH ?g {
    ?transplant a sphn:Transplant ; sphn:hasGraftTypeCode ?code .
    ?code a snomed:7970006 .
  }
}
GROUP BY ?g
"""

# q-010: SQL's FILTER clause after an aggregate, and "a" inside IF()
Q010 = PREFIXES + SNOMED + """SELECT
  (COUNT(DISTINCT ?g) FILTER(?type = "Smoker") AS ?current_smokers)
  (COUNT(DISTINCT ?g) FILTER(?type = "Ex-smoker") AS ?former_smokers)
WHERE {
  GRAPH ?g {
    ?exposure sphn:hasTypeCode ?code .
    BIND(IF(?code a snomed:77176002, "Smoker", IF(?code a snomed:8517006, "Ex-smoker", "Other")) AS ?type)
  }
}
"""

# q-022: a type pattern inside FILTER()
Q022 = PREFIXES + SNOMED + """SELECT ?stay_type (COUNT(DISTINCT ?g) AS ?total_stays)
WHERE {
  {
    SELECT DISTINCT ?g ?type_code
    WHERE {
      GRAPH ?g { ?care a sphn:CareHandling ; sphn:hasTypeCode ?type_code . }
      FILTER (?type_code a snomed:394656005)
      BIND ("Inpatient" AS ?stay_type)
    }
  }
}
GROUP BY ?stay_type
"""

# q-022, first try: a BIND inside the subquery's braces after its WHERE block
Q022_FIRST = PREFIXES + SNOMED + """SELECT ?stay_type (COUNT(DISTINCT ?g) AS ?total_stays)
WHERE {
  {
    SELECT DISTINCT ?g
    WHERE {
      GRAPH ?g { ?stay a sphn:CareHandling ; sphn:hasTypeCode ?type_code . ?type_code a snomed:371883000 . }
    }
    BIND ("Outpatient" AS ?stay_type)
  }
}
GROUP BY ?stay_type
"""

# q-027: a window function and an IF chain missing one parenthesis
Q027 = PREFIXES + """SELECT ?encounter_group (COUNT(DISTINCT ?g) AS ?patient_count)
       (ROUND(1000.0 * COUNT(DISTINCT ?g) / SUM(COUNT(DISTINCT ?g)) OVER ()) / 10 AS ?percentage)
WHERE {
  {
    SELECT ?g (COUNT(DISTINCT ?encounter) AS ?encounter_count)
    WHERE { GRAPH ?g { ?encounter a sphn:HealthcareEncounter . } }
    GROUP BY ?g
  }
  BIND (IF(?encounter_count = 1, "1", IF(?encounter_count <= 5, "2 to 5", "more than 5") AS ?encounter_group)
}
GROUP BY ?encounter_group
"""

# q-046: the first UNION branch is a bare GRAPH block
Q046 = PREFIXES + """SELECT ?type (COUNT(DISTINCT ?g) AS ?patient_count)
WHERE {
  GRAPH ?g {
    ?cnv a sphn:CopyNumberVariation ; sphn:hasTypeCode ?code .
    BIND("gain" AS ?type)
  }
  UNION
  {
    GRAPH ?g {
      ?cnv a sphn:CopyNumberVariation ; sphn:hasTypeCode ?code .
      BIND("loss" AS ?type)
    }
  }
}
GROUP BY ?type
"""

# gen-077: GROUP BY written before the closing brace of WHERE
GEN077 = PREFIXES + """SELECT ?diagnosis_type (COUNT(DISTINCT ?g) AS ?patient_count)
WHERE {
  {
    SELECT DISTINCT ?g ?diagnosis_type
    WHERE { GRAPH ?g { ?record sphn:hasCode ?code . ?code a ?diagnosis_type . } }
  }
  GROUP BY ?diagnosis_type
}
ORDER BY DESC(?patient_count)
"""

# intent-029, first try: the schema card's template "?c a snomed:<code>" copied literally, next to the typed pattern
INTENT029_FIRST = PREFIXES + SNOMED + """SELECT ?snomed_code (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?lab_test) AS ?count)
WHERE {
  GRAPH ?g {
    ?lab_test a sphn:LabTestEvent ;
              sphn:hasLabTest ?lab_test_node .
  }
  {
    ?lab_test_node sphn:hasCode ?code .
    ?code a snomed:<code> OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .
    ?code a ?concept .
  }
  BIND (REPLACE(STR(?concept), "^.*/", "") AS ?snomed_code)
}
GROUP BY ?snomed_code
ORDER BY DESC(?count)
"""

# q-027, first try: the total of the share computed by a subquery inside the SELECT expression
Q027_FIRST = PREFIXES + """SELECT ?group_label
       (COUNT(DISTINCT ?g) AS ?patients)
       (ROUND(1000.0 * COUNT(DISTINCT ?g) / (SELECT COUNT(DISTINCT ?g2) WHERE { GRAPH ?g2 { ?x a sphn:HealthcareEncounter . } })) / 10 AS ?pct)
WHERE {
  {
    SELECT ?g (COUNT(DISTINCT ?encounter) AS ?encounter_count)
    WHERE {
      GRAPH ?g { ?encounter a sphn:HealthcareEncounter . }
    }
    GROUP BY ?g
  }
  BIND (IF(?encounter_count = 1, "1", IF(?encounter_count <= 5, "2 to 5", "more than 5")) AS ?group_label)
}
GROUP BY ?group_label
ORDER BY ?group_label
"""

# official-02, first try: a GROUP BY inside the WHERE block of each UNION branch's sub-select
OFFICIAL02_FIRST = PREFIXES + BIF + """SELECT ?cohort (AVG(?duration_days) AS ?avg_duration)
WHERE {
  {
    SELECT ?g (AVG(bif:datediff('day', ?start, ?end)) AS ?duration_days)
    WHERE {
      GRAPH ?g { ?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?start ; sphn:hasDischargeDateTime ?end . }
      GROUP BY ?g
    }
  }
  UNION
  {
    SELECT ?g (AVG(bif:datediff('day', ?start, ?end)) AS ?duration_days)
    WHERE {
      GRAPH ?g { ?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?start ; sphn:hasDischargeDateTime ?end . }
      GROUP BY ?g
    }
  }
  BIND (IF(EXISTS { GRAPH ?g { ?dx a sphn:BilledDiagnosis } }, "cholera", "other") AS ?cohort)
}
GROUP BY ?cohort
ORDER BY ?cohort
"""

# gen-096: an IF chain missing one closing parenthesis
GEN096 = PREFIXES + """SELECT ?age_decade (COUNT(DISTINCT ?g) AS ?patient_count)
WHERE {
  GRAPH ?g {
    ?age_record a sphn:Age ; sphn:hasQuantity ?age_qty .
    ?age_qty sphn:hasValue ?age_value .
    BIND (IF(?age_value < 10, "0-9",
             IF(?age_value < 20, "10-19",
             IF(?age_value < 30, "20-29", "30+")) AS ?age_decade
  }
}
GROUP BY ?age_decade
"""


def test_diagnoses_class_alternatives_and_constant_group_by():
    assert hints_for(Q008_FIRST) == [
        HINTS["group_by_constant"].format(value="true"),
        HINTS["class_alternatives"],
    ]
    fixed, applied = normalize(Q008_FIRST)
    assert applied == ["dropped GROUP BY true (one overall group)"] and "GROUP BY" not in fixed
    assert diagnose(fixed) == [HINTS["class_alternatives"]]


def test_diagnoses_union_outside_where():
    assert hints_for(Q008_FINAL) == [HINTS["union_outside"]]
    assert normalize(Q008_FINAL) == (Q008_FINAL, [])


def test_diagnoses_filter_after_aggregate():
    assert hints_for(Q010) == [HINTS["aggregate_filter"], HINTS["type_in_expression"]]


def test_diagnoses_type_pattern_inside_filter():
    assert hints_for(Q022) == [HINTS["type_in_expression"]]


def test_diagnoses_subquery_followed_by_bind_inside_its_braces():
    assert hints_for(Q022_FIRST) == [HINTS["subselect_not_alone"]]


def test_diagnoses_window_function_and_missing_parenthesis():
    opened, closed = Q027.count("("), Q027.count(")")
    assert opened == closed + 1
    hints = hints_for(Q027)
    assert hints[0].startswith(f"Parentheses are not balanced: {opened} '(' but {closed} ')'. In BIND(IF(")
    assert hints[1:] == [HINTS["window"]]


def test_diagnoses_if_chain_missing_a_parenthesis():
    assert hints_for(GEN096) == [
        "Parentheses are not balanced: 6 '(' but 4 ')'. In BIND(IF(..., IF(..., ...))) close every IF and then "
        "the BIND. Simpler: BIND(10 * FLOOR(?age / 10) AS ?decade)."
    ]
    without_if = PREFIXES + "SELECT (COUNT(DISTINCT ?g AS ?n) WHERE { GRAPH ?g { ?x a sphn:Age } }"
    assert hints_for(without_if) == [
        "Parentheses are not balanced: 2 '(' but 1 ')'. The '(' that opens \"(COUNT(DISTINCT ?g AS ?n) WHERE { GRAPH "
        "?g { ?x a sphn:Age }...\" is never closed: close every function call inside it and then the (expression AS ?name)."
    ]


def test_parenthesis_hint_names_the_expression_at_fault_outside_an_if_chain():
    # q-071, first try: ROUND(...) misses its ')' while the BIND(IF(...)) further down is fine, so the IF-chain
    # recipe would send the model to rewrite the wrong line
    query = PREFIXES + """SELECT (COUNT(DISTINCT ?g) AS ?patients)
       (ROUND(1000.0 * COUNT(DISTINCT ?g) / ?total / 10 AS ?pct)
       (MAX(?n) AS ?max_tests)
WHERE {
  { SELECT ?g (COUNT(DISTINCT ?e) AS ?n) WHERE { GRAPH ?g { ?e a sphn:LabTestEvent } } GROUP BY ?g }
  BIND (IF(?n >= 10, ?g, ?unbound) AS ?g_with_10)
}
"""
    assert hints_for(query) == [
        "Parentheses are not balanced: 11 '(' but 10 ')'. The '(' that opens \"(ROUND(1000.0 * COUNT(DISTINCT ?g) / "
        "?total / 10 AS ?pct)\" is never closed: close every function call inside it and then the (expression AS ?name)."
    ]
    named_call = PREFIXES + "SELECT ?r WHERE { GRAPH ?g { ?x sphn:hasValue ?v } BIND(ROUND(?v AS ?r) }"
    assert hints_for(named_call)[0].endswith("The '(' that opens \"BIND(ROUND(?v AS ?r) }\" is never closed: close every "
                                             "function call inside it and then the (expression AS ?name).")
    extra = PREFIXES + "SELECT (COUNT(DISTINCT ?g)) AS ?n) WHERE { GRAPH ?g { ?x a sphn:Age } }"
    assert hints_for(extra) == [
        "Parentheses are not balanced: 2 '(' but 3 ')'. The ')' ending \"SELECT (COUNT(DISTINCT ?g)) AS ?n)\" closes "
        "nothing: remove it or open the parenthesis it needs."
    ]


def test_counts_ignore_brackets_inside_strings_iris_and_comments():
    query = PREFIXES + """SELECT (COUNT(DISTINCT ?g) AS ?n)
WHERE { # a comment with ( and {
  GRAPH ?g { ?x a sphn:Age ; sphn:hasValue "(1" ; <http://example.org/p(1)> ?y . }
}
GROUP BY true
"""
    assert hints_for(query) == [HINTS["group_by_constant"].format(value="true")]


def test_unbalanced_braces_hint_replaces_the_structural_ones():
    query = PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n)\nWHERE {\n  GRAPH ?g { ?x a sphn:Age }\nGROUP BY ?x\n"
    assert hints_for(query) == [HINTS["braces"].format(opened=2, closed=1)]
    assert normalize(query) == (query, [])


def test_diagnoses_unbraced_graph_union_branch():
    assert hints_for(Q046) == [HINTS["graph_union"]]
    after_union = PREFIXES + "SELECT ?x WHERE { { ?x a sphn:Age } UNION GRAPH ?g { ?x a sphn:Age } }"
    assert hints_for(after_union) == [HINTS["graph_union"]]


def test_diagnoses_group_by_inside_where():
    assert hints_for(GEN077) == [HINTS["group_by_inside"]]


def test_diagnoses_member_access_and_null():
    query = PREFIXES + BIF + """SELECT (COUNT(DISTINCT ?surgery_patient) AS ?n) (AVG(?wait_days) AS ?wait)
WHERE {
  GRAPH ?g { ?dx a sphn:OncologyDiagnosis ; sphn:hasIncidenceDateTime ?start . OPTIONAL { ?surg a sphn:OncologySurgery } }
  BIND (IF(BOUND(?surg), ?g, NULL) AS ?surgery_patient)
  BIND (bif:datediff("day", ?start, ?surg.sphn:hasStartDateTime) AS ?wait_days)
}
"""
    assert hints_for(query) == [HINTS["member_access"], HINTS["null"]]
    # "or use OPTIONAL" steered the model into OPTIONAL { BIND(...) } blocks (q-055, q-064): the hint now gives
    # the two recipes that work, a per-patient number and an absent pattern
    assert "or use OPTIONAL" not in HINTS["null"]
    assert "SUM(IF(?n >= 10, 1, 0))" in HINTS["null"] and "OPTIONAL { GRAPH ?g { ... } BIND(?g AS ?x) }" in HINTS["null"]


def test_class_alternatives_hint_ignores_a_logical_or():
    # q-022, first try: type patterns joined with || inside FILTER are the type-in-expression mistake, not "A | B"
    query = PREFIXES + SNOMED + """SELECT ?care_type (COUNT(DISTINCT ?g) AS ?stays)
WHERE {
  GRAPH ?g {
    ?ch sphn:hasTypeCode ?code .
    FILTER (?code a snomed:371883000 || ?code a snomed:394656005 || ?code a snomed:304903009)
    BIND (IF(?code a snomed:371883000, "Outpatient", "Inpatient") AS ?care_type)
  }
}
GROUP BY ?care_type
"""
    assert hints_for(query) == [HINTS["type_in_expression"]]


def test_diagnoses_a_card_placeholder_outside_a_type_pattern():
    # The placeholder as a triple's object or a compared value is not repaired (its meaning is not mechanical), so
    # the hint names it; one inside a string is not syntax
    as_object = PREFIXES + SNOMED + "SELECT ?x WHERE { GRAPH ?g { ?x sphn:hasCode snomed:<code> . } }"
    assert hints_for(as_object) == [HINTS["placeholder"].format(term="snomed:<code>")]
    assert normalize(as_object) == (as_object, [])
    compared = PREFIXES + "SELECT ?c WHERE { GRAPH ?g { ?x sphn:hasCode ?c . FILTER (?c = icd-10-gm:<code>) } }"
    assert hints_for(compared) == [HINTS["placeholder"].format(term="icd-10-gm:<code>")]
    in_string = PREFIXES + 'SELECT ?c WHERE { GRAPH ?g { ?x sphn:hasValue "snomed:<code>" } } GROUP BY true'
    assert hints_for(in_string) == [HINTS["group_by_constant"].format(value="true")]


def test_diagnoses_a_scalar_subquery_it_cannot_hoist():
    # the subquery reuses ?g, a variable of the outer query: its meaning per row is not mechanical
    correlated = Q027_FIRST.replace("?g2", "?g")
    assert normalize(correlated) == (correlated, [])
    assert hints_for(correlated) == [HINTS["scalar_subquery"]]
    # inside a GRAPH block, where a block hoisted to the top of WHERE would not be visible to the BIND
    nested = PREFIXES + ("SELECT ?r WHERE { GRAPH ?g { ?x sphn:hasValue ?v . "
                         "BIND (?v / (SELECT COUNT(?y) WHERE { ?y a sphn:Age }) AS ?r) } }")
    assert normalize(nested) == (nested, [])
    assert hints_for(nested) == [HINTS["scalar_subquery"]]
    # with modifiers of its own it is not one value
    grouped = PREFIXES + ("SELECT ((SELECT COUNT(?y) WHERE { ?y a ?t } GROUP BY ?t) AS ?n) "
                          "WHERE { GRAPH ?g { ?x a sphn:Age } }")
    assert normalize(grouped) == (grouped, [])
    assert hints_for(grouped) == [HINTS["scalar_subquery"]]


def test_diagnoses_bare_iri_unprefixed_property_and_unknown_function():
    bare_iri = PREFIXES + "SELECT ?c WHERE { GRAPH ?g { ?r http://purl.org/dc/terms/conformsTo ?c } }"
    assert hints_for(bare_iri) == [HINTS["bare_iri"].format(iri="http://purl.org/dc/terms/conformsTo")]
    bare_property = PREFIXES + "SELECT ?c WHERE { GRAPH ?g { ?sex a sphn:AdministrativeSex . ?sex hasCode ?c } }"
    assert hints_for(bare_property) == ["hasCode has no prefix. Write ?x sphn:hasCode ?value ."]
    random_order = PREFIXES + "SELECT ?s WHERE { GRAPH ?g { ?s a sphn:Age } } ORDER BY RANDOM() LIMIT 5"
    assert hints_for(random_order) == [HINTS["unknown_function"].format(name="RANDOM")]
    median = PREFIXES + "SELECT (MEDIAN(?v) AS ?m) WHERE { GRAPH ?g { ?x sphn:hasValue ?v } }"
    assert hints_for(median) == [HINTS["unknown_function"].format(name="MEDIAN")]


def test_each_hint_appears_once_in_priority_order():
    query = PREFIXES + """SELECT ?type (COUNT(DISTINCT ?g) AS ?n)
WHERE {
  GRAPH ?g { ?x a sphn:Age . BIND("a" AS ?type) }
  UNION
  GRAPH ?g { ?x a sphn:Weight . BIND("b" AS ?type) }
  UNION
  GRAPH ?g { ?x a sphn:Height . BIND("c" AS ?type) }
  FILTER (?x a sphn:Age)
  GROUP BY ?type
}
"""
    assert hints_for(query) == [HINTS["group_by_inside"], HINTS["graph_union"], HINTS["type_in_expression"]]


def test_normalize_drops_a_constant_group_by():
    query = PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n)\nWHERE {\n  GRAPH ?g { ?d a sphn:BilledDiagnosis }\n}\nGROUP BY true\n"
    fixed, applied = repaired(query)
    assert fixed == query.replace("GROUP BY true\n", "") and applied == ["dropped GROUP BY true (one overall group)"]
    inline = PREFIXES + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis } } GROUP BY 1 LIMIT 5"
    fixed, applied = repaired(inline)
    assert "GROUP BY" not in fixed and fixed.endswith("LIMIT 5") and applied == ["dropped GROUP BY 1 (one overall group)"]


def test_normalize_braces_graph_blocks_that_are_union_branches():
    fixed, applied = repaired(Q046)
    assert applied == ["wrapped a GRAPH block that is a UNION branch in braces"]
    assert fixed.count("{ GRAPH ?g {") == 1 and "Union" in operator_chain(parse_sparql(fixed).algebra)
    both_bare = PREFIXES + "SELECT ?x WHERE { GRAPH ?g { ?x a sphn:Age } UNION GRAPH ?g { ?x a sphn:Weight } }"
    fixed, applied = repaired(both_bare)
    assert fixed == PREFIXES + "SELECT ?x WHERE { { GRAPH ?g { ?x a sphn:Age } } UNION { GRAPH ?g { ?x a sphn:Weight } } }"
    assert len(applied) == 2


def test_normalize_moves_group_by_after_the_where_block():
    fixed, applied = repaired(GEN077)
    assert applied == ["moved GROUP BY from inside WHERE to after its closing brace"]
    assert fixed.rindex("}") < fixed.index("GROUP BY ?diagnosis_type") < fixed.index("ORDER BY")
    with_tail = PREFIXES + "SELECT ?t (COUNT(?x) AS ?n) WHERE { GRAPH ?g { ?x a ?t } GROUP BY ?t HAVING (COUNT(?x) > 1) ORDER BY ?t LIMIT 5 }"
    fixed, applied = repaired(with_tail)
    assert fixed.endswith("}\nGROUP BY ?t HAVING (COUNT(?x) > 1) ORDER BY ?t LIMIT 5\n") and len(applied) == 1
    in_subquery = PREFIXES + """SELECT (AVG(?n) AS ?avg)
WHERE {
  {
    SELECT ?g (COUNT(?x) AS ?n)
    WHERE {
      GRAPH ?g { ?x a sphn:Age }
      GROUP BY ?g
    }
  }
}
"""
    fixed, applied = repaired(in_subquery)
    assert applied == ["moved GROUP BY from inside WHERE to after its closing brace"]
    assert re.search(r"\}\s*GROUP BY \?g\s*\}\s*\}\s*$", fixed)


def test_normalize_drops_a_group_by_that_repeats_the_outer_one():
    duplicated = GEN077.replace("}\nORDER BY", "}\nGROUP BY ?diagnosis_type\nORDER BY")
    fixed, applied = repaired(duplicated)
    assert applied == ["dropped a GROUP BY inside WHERE that repeats the one after it"]
    assert fixed.count("GROUP BY") == 1 and fixed.rindex("}") < fixed.index("GROUP BY")


def test_normalize_leaves_a_group_by_mixed_with_patterns_or_in_conflict():
    # official-02, first try: GROUP BY, HAVING and then a BIND inside WHERE; the meaning is not mechanical
    mixed = PREFIXES + """SELECT ?category (COUNT(DISTINCT ?g) AS ?patients)
WHERE {
  { SELECT ?g ?days WHERE { GRAPH ?g { ?x a sphn:Age ; sphn:hasValue ?days } } }
  GROUP BY ?g ?days
  HAVING (COUNT(?x) > 0)
  BIND (IF(?days > 0, "longer", "shorter") AS ?category)
}
GROUP BY ?category
"""
    assert normalize(mixed) == (mixed, []) and hints_for(mixed) == [HINTS["group_by_inside"]]
    # an inner LIMIT cannot precede the outer ORDER BY
    conflict = PREFIXES + "SELECT ?t (COUNT(?x) AS ?n) WHERE { GRAPH ?g { ?x a ?t } GROUP BY ?t LIMIT 5 }\nORDER BY ?n"
    assert normalize(conflict) == (conflict, []) and hints_for(conflict) == [HINTS["group_by_inside"]]


def test_normalize_drops_the_card_placeholder_typed_on_a_code():
    fixed, applied = repaired(INTENT029_FIRST)
    assert applied == ["dropped ?code a snomed:<code>, the card's placeholder (any code)"]
    assert fixed == "\n".join(line for line in INTENT029_FIRST.split("\n") if "snomed:<code>" not in line)
    assert diagnose(fixed) == []
    # inside a predicate chain, opening a chain, closing a block, and written as rdf:type without OPTION
    chain = PREFIXES + SNOMED + "SELECT ?c WHERE { GRAPH ?g { ?x sphn:hasCode ?c ; a snomed:<code>" + INFERENCE + " ; sphn:hasValue ?v . } }"
    assert repaired(chain) == (chain.replace(" ; a snomed:<code>" + INFERENCE, ""),
                               ["dropped a snomed:<code>, the card's placeholder (any code)"])
    opening = PREFIXES + "SELECT ?c WHERE { GRAPH ?g { ?c a icd-10-gm:<code>" + INFERENCE + " ; sphn:hasValue ?v . } }"
    assert repaired(opening) == (opening.replace("a icd-10-gm:<code>" + INFERENCE + " ; ", ""),
                                 ["dropped ?c a icd-10-gm:<code>, the card's placeholder (any code)"])
    closing = PREFIXES + "SELECT ?c WHERE { GRAPH ?g { ?x sphn:hasCode ?c . OPTIONAL { ?c rdf:type chop:<code> } } }"
    assert repaired(closing) == (closing.replace("?c rdf:type chop:<code> ", ""),
                                 ["dropped ?c a chop:<code>, the card's placeholder (any code)"])
    # official-05, first try: the placeholder twice, then NULL; the placeholder goes and NULL gets its hint
    official05 = PREFIXES + SNOMED + """SELECT ?route (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?administered_patient) AS ?administered)
WHERE {
  { SELECT DISTINCT ?g ?route WHERE { GRAPH ?g { ?p a sphn:DrugPrescription ; sphn:hasAdministrationRouteCode ?route . } ?route a snomed:<code>""" + INFERENCE + """ . } }
  OPTIONAL { { SELECT DISTINCT ?g WHERE { GRAPH ?g { ?a a sphn:DrugAdministrationEvent ; sphn:hasAdministrationRouteCode ?route . } ?route a snomed:<code>""" + INFERENCE + """ . } } BIND (?g AS ?administered_patient) }
  BIND (IF(BOUND(?administered_patient), ?g, NULL) AS ?administered_patient)
}
GROUP BY ?route
"""
    fixed, applied = normalize(official05)
    assert applied == ["dropped ?route a snomed:<code>, the card's placeholder (any code)"] * 2
    assert "<code>" not in fixed and parse_sparql(fixed).kind == "Invalid" and diagnose(fixed) == [HINTS["null"]]


def test_normalize_hoists_a_scalar_subquery_into_where():
    fixed, applied = repaired(Q027_FIRST)
    assert applied == ['hoisted the subquery "(SELECT COUNT(DISTINCT ?g2) WHERE { GRAPH ?g2 { ?x a sphn:He..." '
                       'into WHERE as ?scalar_1']
    assert "(ROUND(1000.0 * COUNT(DISTINCT ?g) / ?scalar_1) / 10 AS ?pct)" in fixed
    assert "WHERE {\n  { SELECT (COUNT(DISTINCT ?g2) AS ?scalar_1) WHERE { GRAPH ?g2 { ?x a sphn:HealthcareEncounter . } } }\n  {\n" in fixed
    assert fixed.endswith("}\nGROUP BY ?group_label ?scalar_1\nORDER BY ?group_label\n")
    assert diagnose(fixed) == []
    # the same subquery twice shares one variable; a SELECT of aggregates gets the GROUP BY it lacked, before
    # ORDER BY; an aliased subquery keeps its name
    twice = PREFIXES + ("SELECT (COUNT(DISTINCT ?g) / (SELECT COUNT(DISTINCT ?h) WHERE { GRAPH ?h { ?y a sphn:Age } }) AS ?share)\n"
                        "       ((SELECT COUNT(DISTINCT ?h) WHERE { GRAPH ?h { ?y a sphn:Age } }) AS ?total)\n"
                        "WHERE { GRAPH ?g { ?x a sphn:BilledDiagnosis } }\nORDER BY ?total\n")
    fixed, applied = repaired(twice)
    assert len(applied) == 1 and fixed.count("{ SELECT (COUNT(DISTINCT ?h) AS ?scalar_1) WHERE") == 1
    assert "(COUNT(DISTINCT ?g) / ?scalar_1 AS ?share)" in fixed and "(?scalar_1 AS ?total)" in fixed
    assert fixed.endswith("WHERE {\n  { SELECT (COUNT(DISTINCT ?h) AS ?scalar_1) WHERE { GRAPH ?h { ?y a sphn:Age } } } "
                          "GRAPH ?g { ?x a sphn:BilledDiagnosis } }\nGROUP BY ?scalar_1\nORDER BY ?total\n")
    aliased = PREFIXES + ("SELECT ?v ((SELECT (COUNT(?y) AS ?all) WHERE { ?y a sphn:Age }) AS ?total) "
                          "WHERE { GRAPH ?g { ?x sphn:hasValue ?v } }")
    fixed, applied = repaired(aliased)
    assert "(?all AS ?total)" in fixed and "{ SELECT (COUNT(?y) AS ?all) WHERE { ?y a sphn:Age } }" in fixed
    assert "GROUP BY" not in fixed  # nothing is aggregated in the outer query


def test_normalize_prefixes_a_bare_property_the_data_has():
    # q-032, dropped retry: hasSpecialtyName without its prefix was the only error; the cards say it is a data term
    query = PREFIXES + ("SELECT ?name (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?e a sphn:HealthcareEncounter ; "
                        "sphn:hasTherapeuticArea ?area . ?area hasSpecialtyName ?name } } GROUP BY ?name\n")
    assert normalize(query) == (query, [])  # without the data's terms nothing is guessed
    assert hints_for(query) == [HINTS["bare_property"].format(term="hasSpecialtyName")]
    fixed, applied = repaired(query, DATA_TERMS)
    assert fixed == query.replace("?area hasSpecialtyName", "?area sphn:hasSpecialtyName")
    assert applied == ["wrote sphn:hasSpecialtyName for the bare hasSpecialtyName"]
    # a name the data lacks stays for the hint, and the term check once the model has prefixed it
    assert normalize(query.replace("hasSpecialtyName", "hasSpecialityName"), DATA_TERMS)[1] == []
    # the sphn: prefix is declared when the query had none, and a string is not a term
    bare = 'SELECT ?n WHERE { ?x hasCode ?n ; sphn:hasValue "hasCode" }'
    fixed, applied = normalize(bare, {"hasCode"})
    assert fixed.startswith("PREFIX sphn: <") and fixed.endswith('?x sphn:hasCode ?n ; sphn:hasValue "hasCode" }')
    assert parse_sparql(fixed).kind == "SelectQuery"


def test_normalize_moves_every_misplaced_group_by():
    fixed, applied = repaired(OFFICIAL02_FIRST)
    assert applied == ["moved GROUP BY from inside WHERE to after its closing brace"] * 2
    assert len(re.findall(r"\}\s*GROUP BY \?g\s*\}", fixed)) == 2 and "GROUP BY ?g\n    }" not in fixed


@pytest.mark.parametrize("query", [query for _, query in VALID_QUERIES], ids=[qid for qid, _ in VALID_QUERIES])
def test_valid_queries_get_no_hints_and_are_left_byte_identical(query):
    assert diagnose(query) == []
    assert normalize(query, DATA_TERMS) == (query, [])
    assert fix_standard_prefixes(query) == query


def test_judge_flow_rescues_or_explains():
    # The flow documented in the module docstring, as the judge wires it.
    for query, expected in ((Q046, "SelectQuery"), (Q027, "Invalid")):
        fixed = add_missing_prefixes(fix_standard_prefixes(query))
        parsed = parse_sparql(fixed)
        assert parsed.kind == "Invalid"
        repaired_text, applied = normalize(fixed)
        parsed = parse_sparql(repaired_text)
        assert parsed.kind == expected
        if expected == "Invalid":
            assert applied == [] and (diagnose(repaired_text) or [parsed.error])[1] == HINTS["window"]
        else:
            assert applied and parsed.query is not None
