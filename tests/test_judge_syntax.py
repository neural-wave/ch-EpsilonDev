import json
import re
from pathlib import Path

import pytest

from conftest import PREFIXES, load_examples
from nl2sparql.judge_checks.syntax import HINTS, diagnose, normalize
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes, fix_standard_prefixes

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
SNOMED = "PREFIX snomed: <http://snomed.info/id/>\n"
BIF = "PREFIX bif: <bif:>\n"


def hints_for(query: str) -> list[str]:
    assert parse_sparql(query).kind == "Invalid"
    return diagnose(query)


def repaired(query: str) -> tuple[str, list[str]]:
    assert parse_sparql(query).kind == "Invalid"
    fixed, applied = normalize(query)
    assert parse_sparql(fixed).kind == "SelectQuery", fixed
    return fixed, applied


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


VALID_QUERIES = [(example["id"], example["sparql"]) for example in load_examples()] + dev_reference_queries()


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
        "Parentheses are not balanced: 2 '(' but 1 ')'. Close every function call and every (expression AS ?name)."
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


@pytest.mark.parametrize("query", [query for _, query in VALID_QUERIES], ids=[qid for qid, _ in VALID_QUERIES])
def test_valid_queries_get_no_hints_and_are_left_byte_identical(query):
    assert diagnose(query) == []
    assert normalize(query) == (query, [])
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
