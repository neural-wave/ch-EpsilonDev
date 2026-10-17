"""Work out what kind of value every variable in a query can hold.

The guard does not care about variable names. It cares whether a variable can carry a patient
identifier, a record IRI, an exact timestamp, a measured value or just a category. We derive that
from the triple patterns that bind the variable, then propagate it through BIND and aggregates.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF, RDFS, SKOS
from rdflib.paths import AlternativePath, MulPath, SequencePath
from rdflib.plugins.sparql.parserutils import CompValue

SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
TIMESTAMP_DATATYPES = {
    "http://www.w3.org/2001/XMLSchema#dateTime",
    "http://www.w3.org/2001/XMLSchema#date",
}
CATEGORY_PREDICATES = {str(RDF.type), str(RDFS.subClassOf), str(RDFS.label), str(SKOS.prefLabel), str(SKOS.altLabel)}

# Functions whose result still carries the identity of their input (STR(?g) is still the graph name).
IDENTITY_PRESERVING = {
    "Builtin_STR", "Builtin_STRAFTER", "Builtin_STRBEFORE", "Builtin_REPLACE", "Builtin_SUBSTR",
    "Builtin_CONCAT", "Builtin_LCASE", "Builtin_UCASE", "Builtin_ENCODE_FOR_URI", "Builtin_IRI",
    "Builtin_STRDT", "Builtin_STRLANG", "Builtin_COALESCE", "Builtin_MD5", "Builtin_SHA1",
    "Builtin_SHA256", "Builtin_SHA384", "Builtin_SHA512",
}
VALUE_AGGREGATES = {"Aggregate_Count", "Aggregate_Sum", "Aggregate_Avg"}
EXISTS = {"Builtin_EXISTS", "Builtin_NOTEXISTS"}


def exists_pattern(node: CompValue):
    """The translated pattern of an EXISTS / NOT EXISTS node.

    rdflib's translateExists assigns the translated pattern to the attribute ``graph`` of the node, so
    ``node.items()`` still holds the raw parse tree, from which the inner FILTERs have been removed.
    Walking the items would miss every GRAPH variable, triple and filter inside the EXISTS.
    """
    return node.graph


class Sensitivity(IntEnum):
    CONSTANT = 0    # literals, VALUES rows, values computed from constants only
    CATEGORY = 1    # classes, code concepts, value-set individuals, labels
    VALUE = 2       # measured values and numbers derived from data
    TIMESTAMP = 3   # exact dates and times
    INSTANCE = 4    # record and code-node IRIs, unique per patient record
    IDENTIFIER = 5  # patient graph names and identifier literals


@dataclass
class PropertyKinds:
    """What each property points to, measured on the data by scripts/profile_data.py."""

    kinds: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "PropertyKinds":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def object_sensitivity(self, predicate: URIRef) -> Sensitivity:
        iri = str(predicate)
        local = iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if iri in CATEGORY_PREDICATES:
            return Sensitivity.CATEGORY
        if "Identifier" in local:
            return Sensitivity.IDENTIFIER
        info = self.kinds.get(iri)
        if info is not None:
            if info["kind"] == "literal":
                if info.get("datatype") in TIMESTAMP_DATATYPES or local.endswith("DateTime"):
                    return Sensitivity.TIMESTAMP
                return Sensitivity.VALUE
            if info["kind"] == "individual":
                return Sensitivity.CATEGORY
            return Sensitivity.INSTANCE
        if local.endswith("DateTime"):
            return Sensitivity.TIMESTAMP
        if iri.startswith(SPHN):
            return Sensitivity.INSTANCE  # unknown SPHN property: assume it links to a record
        return Sensitivity.VALUE


@dataclass
class TriplePattern:
    subject: object
    predicate: object
    object: object
    in_ontology_graph: bool


@dataclass
class QueryFacts:
    sensitivity: dict[Variable, Sensitivity] = field(default_factory=dict)
    aggregates: dict[Variable, CompValue] = field(default_factory=dict)
    triples: list[TriplePattern] = field(default_factory=list)
    iris: set[URIRef] = field(default_factory=set)
    filters: list[object] = field(default_factory=list)
    services: list[object] = field(default_factory=list)
    touches_patient_data: bool = False
    type_objects: set[Variable] = field(default_factory=set)  # objects of rdf:type: always classes

    def raise_to(self, variable: Variable, level: Sensitivity) -> None:
        if variable in self.type_objects:
            level = min(level, Sensitivity.CATEGORY)
        self.sensitivity[variable] = max(self.sensitivity.get(variable, Sensitivity.CONSTANT), level)


def analyse(algebra: CompValue, kinds: PropertyKinds, ontology_graphs: frozenset[str]) -> QueryFacts:
    facts = QueryFacts()
    _collect_type_objects(algebra, facts.type_objects)
    _visit(algebra, facts, kinds, ontology_graphs, in_ontology=False)
    return facts


def _collect_type_objects(node, found: set[Variable]) -> None:
    """In SPHN data the object of rdf:type is a class or a code concept, never a patient record."""
    if isinstance(node, (list, tuple, set)):
        for item in node:
            _collect_type_objects(item, found)
    elif isinstance(node, CompValue):
        if node.name == "BGP":
            found.update(o for _, p, o in node.triples if p == RDF.type and isinstance(o, Variable))
            return
        if node.name in EXISTS:
            _collect_type_objects(exists_pattern(node), found)
            return
        for key, value in node.items():
            if not key.startswith("_"):
                _collect_type_objects(value, found)


def expression_sensitivity(expr: object, facts: QueryFacts) -> Sensitivity:
    if isinstance(expr, Variable):
        return facts.sensitivity.get(expr, Sensitivity.VALUE)
    if expr is None or isinstance(expr, (Literal, URIRef, str)):
        return Sensitivity.CONSTANT
    if isinstance(expr, (list, tuple)):
        return max((expression_sensitivity(item, facts) for item in expr), default=Sensitivity.CONSTANT)
    if isinstance(expr, CompValue):
        if expr.name == "Builtin_IF":
            return max(expression_sensitivity(expr.arg2, facts), expression_sensitivity(expr.arg3, facts))
        if expr.name in IDENTITY_PRESERVING:
            children = [value for key, value in expr.items() if not key.startswith("_")]
            return max((expression_sensitivity(child, facts) for child in children), default=Sensitivity.CONSTANT)
        return Sensitivity.VALUE  # numbers, booleans, date parts, comparisons
    return Sensitivity.VALUE


def _predicate_object_sensitivity(predicate: object, kinds: PropertyKinds) -> Sensitivity:
    if isinstance(predicate, URIRef):
        return kinds.object_sensitivity(predicate)
    if isinstance(predicate, SequencePath):
        return _predicate_object_sensitivity(predicate.args[-1], kinds)
    if isinstance(predicate, AlternativePath):
        return max(_predicate_object_sensitivity(arg, kinds) for arg in predicate.args)
    if isinstance(predicate, MulPath):
        return _predicate_object_sensitivity(predicate.path, kinds)
    return Sensitivity.INSTANCE  # variable, inverse or negated predicate: could reach any record


def _record_triple(triple, facts: QueryFacts, kinds: PropertyKinds, in_ontology: bool) -> None:
    subject, predicate, obj = triple
    facts.triples.append(TriplePattern(subject, predicate, obj, in_ontology))
    for term in triple:
        if isinstance(term, URIRef):
            facts.iris.add(term)
    if not in_ontology:
        facts.touches_patient_data = True
    if isinstance(subject, Variable):
        facts.raise_to(subject, Sensitivity.CATEGORY if in_ontology else Sensitivity.INSTANCE)
    if isinstance(predicate, Variable):
        facts.raise_to(predicate, Sensitivity.CATEGORY)
    if isinstance(obj, Variable):
        level = Sensitivity.CATEGORY if in_ontology else _predicate_object_sensitivity(predicate, kinds)
        facts.raise_to(obj, level)


def _visit(node, facts: QueryFacts, kinds: PropertyKinds, ontology_graphs: frozenset[str], in_ontology: bool) -> None:
    if isinstance(node, URIRef):
        facts.iris.add(node)
        return
    if isinstance(node, (list, tuple, set)):
        for item in node:
            _visit(item, facts, kinds, ontology_graphs, in_ontology)
        return
    if not isinstance(node, CompValue):
        return
    if node.name == "Graph":
        term = node.term
        if isinstance(term, Variable):
            facts.raise_to(term, Sensitivity.IDENTIFIER)
            _visit(node.p, facts, kinds, ontology_graphs, in_ontology=False)
        else:
            facts.iris.add(term)
            _visit(node.p, facts, kinds, ontology_graphs, in_ontology=str(term) in ontology_graphs)
        return
    if node.name == "BGP":
        for triple in node.triples:
            _record_triple(triple, facts, kinds, in_ontology)
        return
    if node.name == "values":
        for row in node.res:
            for variable, term in row.items():
                facts.raise_to(variable, Sensitivity.CONSTANT)
                if isinstance(term, URIRef):
                    facts.iris.add(term)
        return
    if node.name in EXISTS:
        _visit(exists_pattern(node), facts, kinds, ontology_graphs, in_ontology)
        return
    if node.name == "ServiceGraphPattern":
        facts.services.append(node.term)
    if node.name == "Filter":
        facts.filters.append(node.expr)
    for key, value in node.items():
        if key.startswith("_") or key == "PV":
            continue
        _visit(value, facts, kinds, ontology_graphs, in_ontology)
    if node.name == "Extend":
        facts.raise_to(node.var, expression_sensitivity(node.expr, facts))
    elif node.name == "AggregateJoin":
        # rdflib names aggregate results __agg_1__, __agg_2__, ... per (sub)query, so names repeat
        # across scopes. Post-order traversal reaches each scope's Extend nodes right after its own
        # AggregateJoin, so overwriting (instead of taking the max) keeps every scope correct.
        for aggregate in node.A:
            facts.aggregates[aggregate.res] = aggregate
            if aggregate.name in VALUE_AGGREGATES:
                facts.sensitivity[aggregate.res] = Sensitivity.VALUE
            else:
                facts.sensitivity[aggregate.res] = expression_sensitivity(aggregate.vars, facts)
