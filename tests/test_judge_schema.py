import copy
from pathlib import Path

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.context import SchemaCards
from nl2sparql.judge_checks.schema import (
    CHECKS,
    REWRITES,
    check_class_property,
    check_code_system,
    check_record_link_typed,
    check_typed_value_set,
    check_value_set_member,
    rewrite_typed_value_set,
    suggest_missing_term,
)
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GRAPH_COUNT = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { %s } }"


@pytest.fixture(scope="module")
def schema() -> SchemaCards:
    return SchemaCards.load(DATA_DIR / "schema_cards.json", FIXTURES / "value_sets_small.json")


def run(check, query: str, schema: SchemaCards) -> list[str]:
    text = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(text)
    assert parsed.algebra is not None, parsed.error
    return check(parsed, text, schema)


def cards(classes: dict) -> SchemaCards:
    return SchemaCards({name: {"instances": 1, "patients": 1, "properties": props} for name, props in classes.items()}, [])


def test_class_property_lists_the_properties_of_the_class(schema):
    query = GRAPH_COUNT % "?stay a sphn:AdministrativeCase ; sphn:hasStartDateTime ?start ; sphn:hasEndDateTime ?end ."
    assert run(check_class_property, query, schema) == [
        "sphn:AdministrativeCase has no sphn:hasStartDateTime and sphn:hasEndDateTime in the data. "
        "Its properties: sphn:hasAdmissionDateTime, sphn:hasCareHandling -> sphn:CareHandling, sphn:hasDischargeDateTime, "
        "sphn:hasDischargeLocation -> sphn:Location, sphn:hasIdentifier, sphn:hasOriginLocation -> sphn:Location."
    ]
    assert run(check_class_property, GRAPH_COUNT % "?drug a sphn:Drug ; sphn:hasName ?name .", schema) == [
        "sphn:Drug has no sphn:hasName in the data. Its properties: sphn:hasActiveIngredient -> sphn:Substance, "
        "sphn:hasArticle -> sphn:DrugArticle, sphn:hasInactiveIngredient -> sphn:Substance, sphn:hasQuantity -> sphn:Quantity."
    ]


def test_class_property_names_the_one_close_replacement():
    stems = cards({"Stay": {"hasRank": {"kind": "individual"}}, "Other": {"hasRankCode": {"kind": "resource"}}})
    assert run(check_class_property, GRAPH_COUNT % "?s a sphn:Stay ; sphn:hasRankCode ?r .", stems) == [
        "sphn:Stay has no sphn:hasRankCode in the data. Write sphn:hasRank instead of sphn:hasRankCode. Its properties: sphn:hasRank."
    ]
    close = cards({
        "Stay": {"hasAdmissionDateTime": {"kind": "literal"}, "hasDischargeDateTime": {"kind": "literal"}},
        "Other": {"hasAdmissionDate": {"kind": "literal"}},
    })
    assert run(check_class_property, GRAPH_COUNT % "?s a sphn:Stay ; sphn:hasAdmissionDate ?d .", close) == [
        "sphn:Stay has no sphn:hasAdmissionDate in the data. Write sphn:hasAdmissionDateTime instead of sphn:hasAdmissionDate. "
        "Its properties: sphn:hasAdmissionDateTime, sphn:hasDischargeDateTime."
    ]


def test_class_property_ignores_unknown_terms_and_other_typings(schema):
    # A property that occurs nowhere is the term check's business.
    assert run(check_class_property, GRAPH_COUNT % "?s a sphn:AdministrativeCase ; sphn:hasRankCode ?r .", schema) == []
    variable = "VALUES ?cls { sphn:AdministrativeCase } ?s a ?cls ; sphn:hasStartDateTime ?t ."
    assert run(check_class_property, GRAPH_COUNT % variable, schema) == []
    twice = "?s a sphn:AdministrativeCase, sphn:HealthcareEncounter ; sphn:hasStartDateTime ?t ."
    assert run(check_class_property, GRAPH_COUNT % twice, schema) == []
    # A concrete class decides even when the subject is also typed by a variable (q-076).
    concrete = "?drug a sphn:Drug ; sphn:hasName ?name . ?drug a ?drug_concept ."
    assert len(run(check_class_property, GRAPH_COUNT % concrete, schema)) == 1


def test_class_property_scopes_union_branches_and_sees_optional_and_exists(schema):
    branches = (
        "{ ?x a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?t } "
        "UNION { ?x a sphn:HealthcareEncounter ; sphn:hasStartDateTime ?t }"
    )
    assert run(check_class_property, GRAPH_COUNT % branches, schema) == []
    optional = "?x a sphn:AdministrativeCase . OPTIONAL { ?x sphn:hasStartDateTime ?t }"
    assert run(check_class_property, GRAPH_COUNT % optional, schema)[0].startswith("sphn:AdministrativeCase has no sphn:hasStartDateTime")
    exists = (
        "SELECT ?cohort (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase } "
        "BIND (IF(EXISTS { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasAdministrativeCase ?case ; sphn:hasStartDateTime ?s } }, "
        '"yes", "no") AS ?cohort) } GROUP BY ?cohort'
    )
    assert run(check_class_property, exists, schema)[0].startswith("sphn:BilledDiagnosis has no sphn:hasStartDateTime")


def test_record_link_typed_as_a_code_node(schema):
    query = (
        "SELECT ?specialty_concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?encounter a sphn:HealthcareEncounter ; sphn:hasTherapeuticArea ?area_code . ?area_code a ?specialty_concept . } } "
        "GROUP BY ?specialty_concept"
    )
    assert run(check_record_link_typed, query, schema) == [
        "?area_code is a sphn:TherapeuticArea record (object of hasTherapeuticArea), not a code node: "
        "'?area_code a ?specialty_concept' only yields that class. "
        "Use its own properties: hasSpecialtyName (value set, group by it directly)."
    ]
    intent = GRAPH_COUNT % "?proc sphn:hasIntent ?intent_code . ?intent_code a ?intent_concept . FILTER (?intent_concept IN (snomed:373808002))"
    assert run(check_record_link_typed, intent, schema) == [
        "?intent_code is a sphn:Intent record (object of hasIntent), not a code node: "
        "'?intent_code a ?intent_concept' only yields that class. "
        "Use its own properties: hasCode (code node: ?r sphn:hasCode ?c . ?c a ?concept)."
    ]


def test_record_link_exempts_values_several_targets_and_code_links(schema):
    reference = next(example for example in load_examples() if example["id"] == "official-08")
    assert run(check_record_link_typed, reference["sparql"], schema) == []
    results = GRAPH_COUNT % "?test a sphn:LabTestEvent ; sphn:hasResult ?result . ?result a ?kind ."
    assert run(check_record_link_typed, results, schema) == []
    code = GRAPH_COUNT % "?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?concept ."
    assert run(check_record_link_typed, code, schema) == []
    listed = cards({
        "Descriptor": {"hasVariation": {"kind": "resource", "target": "Snv", "targets": ["Snv", "Deletion"]}},
        "Snv": {"hasAllele": {"kind": "literal"}}, "Deletion": {"hasAllele": {"kind": "literal"}},
    })
    assert run(check_record_link_typed, GRAPH_COUNT % "?d sphn:hasVariation ?v . ?v a ?cls .", listed) == []
    single = cards({"Descriptor": {"hasVariation": {"kind": "resource", "targets": ["Snv"]}}, "Snv": {"hasAllele": {"kind": "literal", "datatype": "string"}}})
    assert run(check_record_link_typed, GRAPH_COUNT % "?d sphn:hasVariation ?v . ?v a ?cls .", single) == [
        "?v is a sphn:Snv record (object of hasVariation), not a code node: '?v a ?cls' only yields that class. "
        "Use its own properties: hasAllele (string)."
    ]


Q044 = """PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT ?fixation_concept (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?specimen) AS ?specimens)
WHERE {
  GRAPH ?g {
    ?specimen a sphn:TumorSpecimen ;
              sphn:hasFixationType ?fixation_type .
    ?fixation_type a ?fixation_concept .
  }
}
GROUP BY ?fixation_concept
ORDER BY DESC(?patients)
LIMIT 5
"""


def test_rewrite_typed_value_set_drops_the_type_triple_and_renames(schema):
    rewritten, repairs = rewrite_typed_value_set(Q044, schema)
    assert repairs == [
        "Removed '?fixation_type a ?fixation_concept' and replaced ?fixation_concept by ?fixation_type: "
        "sphn:hasFixationType holds a value-set entry (sphn-individual:...), which is the category itself, not a code node."
    ]
    assert "?fixation_concept" not in rewritten and "?fixation_type a" not in rewritten
    assert "SELECT ?fixation_type" in rewritten and "GROUP BY ?fixation_type" in rewritten
    parsed = parse_sparql(rewritten)
    assert parsed.kind == "SelectQuery"
    assert check_typed_value_set(parsed, rewritten, schema) == []


@pytest.mark.parametrize("body, expected", [
    ("?s sphn:hasFixationType ?f . ?f a ?c ; rdfs:label ?l .", "?s sphn:hasFixationType ?f . ?f rdfs:label ?l ."),
    ("?s sphn:hasFixationType ?f . ?f rdfs:label ?l ; a ?c .", "?s sphn:hasFixationType ?f . ?f rdfs:label ?l ."),
    ("?s sphn:hasFixationType ?f . ?f rdf:type ?c", "?s sphn:hasFixationType ?f . "),
    ('?s sphn:hasFixationType ?f . ?f a ?c OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .', "?s sphn:hasFixationType ?f . "),
])
def test_rewrite_handles_chains_and_the_last_statement(schema, body, expected):
    query = add_missing_prefixes("SELECT ?f WHERE { GRAPH ?g { %s } }" % body)  # the judge declares prefixes first
    rewritten, repairs = rewrite_typed_value_set(query, schema)
    assert len(repairs) == 1
    assert rewritten == query.replace(body, expected)


def test_rewrite_leaves_the_query_when_it_cannot_do_it_cleanly(schema):
    both = "SELECT ?f ?c WHERE { GRAPH ?g { ?s sphn:hasFixationType ?f . ?f a ?c } }"
    assert rewrite_typed_value_set(both, schema) == (both, [])
    assert run(check_typed_value_set, both, schema) == [
        "sphn:hasFixationType holds a value-set entry (sphn-individual:...), not a code node: "
        "drop '?f a ...' and group by or compare ?f itself."
    ]
    code = "SELECT ?concept WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?concept } }"
    assert rewrite_typed_value_set(code, schema) == (code, [])
    assert run(check_typed_value_set, code, schema) == []
    assert rewrite_typed_value_set("SELECT ?x WHERE {", schema) == ("SELECT ?x WHERE {", [])


def test_value_set_member_on_the_wrong_class(schema):
    q039 = (
        "SELECT ?outcome_concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?follow_up a sphn:FollowUp ; sphn:hasCode ?code ; sphn:hasResult ?vital_status . } ?code a ?outcome_concept . "
        "FILTER (?outcome_concept = snomed:438949009 || ?outcome_concept = snomed:399307001) } GROUP BY ?outcome_concept"
    )
    assert run(check_value_set_member, q039, schema) == [
        "snomed:399307001 (Lost to follow-up) and snomed:438949009 (Alive) are not values of sphn:FollowUp hasCode "
        "(its values: Telephone follow-up snomed:386473003 | Follow-up consultation snomed:281036007). "
        "They are values of sphn:VitalStatus hasCode: write ?follow_up sphn:hasResult ?vital_status . "
        "?vital_status sphn:hasCode ?code . ?code a ?concept."
    ]
    values = GRAPH_COUNT % (
        "?follow_up a sphn:FollowUp ; sphn:hasCode ?fu_code . ?fu_code a ?fu_concept . "
        'VALUES (?fu_concept ?group_name) { (snomed:438949009 "Alive") }'
    )
    assert run(check_value_set_member, values, schema)[0].startswith("snomed:438949009 (Alive) is not a value of sphn:FollowUp hasCode")
    typed = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:438949009 ."
    assert len(run(check_value_set_member, typed, schema)) == 1
    rank = GRAPH_COUNT % "?proc a sphn:BilledProcedure ; sphn:hasRank sphn-individual:Secondary ."
    assert run(check_value_set_member, rank, schema) == [
        "sphn-individual:Secondary (Secondary) is not a value of sphn:BilledProcedure hasRank "
        "(its values: Principal sphn-individual:Principal | Supplementary sphn-individual:Supplementary). "
        "It is a value of sphn:BilledDiagnosis hasRank: write ?proc a sphn:BilledDiagnosis ; sphn:hasRank sphn-individual:Secondary."
    ]


def test_value_set_member_stays_silent_without_evidence(schema):
    own = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:386473003 ."
    assert run(check_value_set_member, own, schema) == []
    unknown = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:1 ."
    assert run(check_value_set_member, unknown, schema) == []
    untyped = GRAPH_COUNT % "?follow_up sphn:hasCode ?code . ?code a snomed:438949009 ."
    assert run(check_value_set_member, untyped, schema) == []
    without_sets = SchemaCards.load(DATA_DIR / "schema_cards.json")
    typed = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:438949009 ."
    assert run(check_value_set_member, typed, without_sets) == []


@pytest.fixture(scope="module")
def coded(schema) -> SchemaCards:
    """The cards with the per-property 'code_system' field another agent is adding."""
    classes = copy.deepcopy(schema.classes)
    systems = {"BilledProcedure": "CHOP", "Diagnosis": "generic", "BilledDiagnosis": "ICD-10-GM",
               "OncologyDiagnosis": "ICD-10-GM", "RadiotherapyProcedure": "SNOMED CT"}
    for name, system in systems.items():
        classes[name]["properties"]["hasCode"]["code_system"] = system
    return SchemaCards(classes, list(schema.schema_terms), schema.value_sets)


def test_code_system_against_the_class(coded):
    chop = GRAPH_COUNT % '?t a sphn:BilledProcedure ; sphn:hasCode ?tc . ?tc a snomed:33195004 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .'
    assert run(check_code_system, chop, coded) == [
        "sphn:BilledProcedure codes are CHOP (https://biomedit.ch/rdf/sphn-resource/chop/), not SNOMED CT."
    ]
    namespace = (
        "SELECT ?concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?billed a sphn:BilledProcedure ; sphn:hasCode ?code . "
        '?code a ?concept . } FILTER (STRSTARTS(STR(?concept), "http://snomed.info/id/")) } GROUP BY ?concept'
    )
    assert len(run(check_code_system, namespace, coded)) == 1
    generic = GRAPH_COUNT % '?diag a sphn:Diagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:C18 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .'
    assert run(check_code_system, generic, coded) == [
        "sphn:Diagnosis codes are generic sphn:Code nodes in the data; ICD-10-GM codes are on sphn:BilledDiagnosis and sphn:OncologyDiagnosis."
    ]


def test_code_system_is_silent_when_the_system_matches_or_is_unknown(coded, schema):
    own = (
        "SELECT ?concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . "
        "?code a ?concept . } FILTER (STRSTARTS(STR(?concept), STR(chop:))) } GROUP BY ?concept"
    )
    assert run(check_code_system, own, coded) == []
    snomed = GRAPH_COUNT % "?t a sphn:RadiotherapyProcedure ; sphn:hasCode ?tc . ?tc a snomed:33195004 ."
    assert run(check_code_system, snomed, coded) == []
    chop = GRAPH_COUNT % "?t a sphn:BilledProcedure ; sphn:hasCode ?tc . ?tc a snomed:33195004 ."
    bare = cards({"BilledProcedure": {"hasCode": {"kind": "resource"}}})
    assert run(check_code_system, chop, bare) == []  # no code_system on the cards: nothing to say


def test_suggest_missing_term_renders_the_class_pattern_or_a_near_identical_property(schema):
    assert suggest_missing_term("hasSex", schema) == (
        "sphn:hasSex does not occur in the data. Sex is a record: ?s a sphn:AdministrativeSex ; sphn:hasCode ?c . "
        "?c a ?concept. Never return ?c."
    )
    assert suggest_missing_term("hasRankCode", schema) == "sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?"
    age = suggest_missing_term("hasAge", schema)
    assert age.startswith(
        "sphn:hasAge does not occur in the data. Age is a record: ?s a sphn:Age ; sphn:hasQuantity ?quantity . ?quantity sphn:hasValue ?value."
    )
    assert "sphn:hasQuantity -> sphn:Quantity" in age
    diagnosis = suggest_missing_term("hasDiagnosis", schema)
    assert "?s a sphn:Diagnosis ; sphn:hasCode ?c . ?c a ?concept (also sphn:BilledDiagnosis)" in diagnosis
    assert suggest_missing_term("hasFooBarBaz", schema) == "sphn:hasFooBarBaz does not occur in the data."
    assert suggest_missing_term("hasSex", SchemaCards({}, [])) == "sphn:hasSex does not occur in the data."


def test_checks_are_silent_on_the_official_examples_and_without_cards(schema):
    for example in load_examples():
        text = add_missing_prefixes(example["sparql"].strip() + "\n")
        for rewrite in REWRITES:
            assert rewrite(text, schema) == (text, []), example["id"]
        parsed = parse_sparql(text)
        assert parsed.algebra is not None, example["id"]
        for check in CHECKS:
            assert check(parsed, text, schema) == [], (example["id"], check.__name__)
    query = GRAPH_COUNT % "?stay a sphn:AdministrativeCase ; sphn:hasStartDateTime ?t . ?stay sphn:hasTherapeuticArea ?a . ?a a ?cls ."
    for check in CHECKS:
        assert run(check, query, SchemaCards({}, [])) == []
    assert rewrite_typed_value_set(query, SchemaCards({}, [])) == (query, [])


def test_module_lists_every_check_and_rewrite():
    assert CHECKS == [check_class_property, check_record_link_typed, check_typed_value_set, check_value_set_member, check_code_system]
    assert REWRITES == [rewrite_typed_value_set]
