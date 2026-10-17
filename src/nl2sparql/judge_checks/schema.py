"""Judge checks that read the schema cards.

A property used on a class whose records do not carry it, a record link typed as if it were a code
node, a value-set entry typed or placed on the wrong class, and a code system the class does not use
are all valid SPARQL that answers empty or wrong rows on the real data. Every check here prefers a
miss to a false alarm, because a false alarm costs the request its only retry: a hint fires only when
the cards say the pattern cannot match (one class per record, one target per link, a known value set).

Variables are matched by name across the whole query, except across the branches of a UNION, which
never join. rdflib traps: Variable is a str subclass, so isinstance checks come before str checks;
inside EXISTS the triples are the parse tree's flat term lists, not BGP nodes.
"""
from __future__ import annotations

import difflib
import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.context.schema import SchemaCards, words
from nl2sparql.sparql.parse import ParsedQuery, parse_sparql
from nl2sparql.sparql.prefixes import SPHN, STANDARD_PREFIXES

BOILERPLATE = ("hasDataProvider", "hasSourceSystem", "hasSubjectPseudoIdentifier")
CODE_TARGETS = ("Code", "Terminology")
CODE_SYSTEMS = {  # the "code_system" names of the cards and the namespace each one's codes live in
    "ICD-10-GM": STANDARD_PREFIXES["icd-10-gm"],
    "CHOP": STANDARD_PREFIXES["chop"],
    "SNOMED CT": STANDARD_PREFIXES["snomed"],
    "LOINC": STANDARD_PREFIXES["loinc"],
    "UCUM": STANDARD_PREFIXES["ucum"],
}
HIERARCHICAL_NAMESPACES = (STANDARD_PREFIXES["icd-10-gm"], STANDARD_PREFIXES["chop"])
REPLACEMENT_RATIO = 0.75  # one property of the class this close to the missing one is named as its replacement
SUGGESTION_RATIO = 0.8  # "Did you mean" needs near-identical names: hasSample is never offered for hasSex
TYPE_RE = r"(?:a|rdf:type)"
OPTION_RE = r"(?:\s*OPTION\s*\([^)]*\))?"


@dataclass(frozen=True)
class Pattern:
    subject: object
    predicate: object
    object: object
    branch: tuple  # ((union id, branch index), ...): two patterns in different branches of one UNION never join

    def joins(self, other: "Pattern") -> bool:
        chosen = dict(self.branch)
        return all(chosen.get(union, index) == index for union, index in other.branch)


class _Analysis:
    """Triple patterns, VALUES bindings and expressions of one parsed query."""

    def __init__(self, algebra, schema: SchemaCards):
        self.schema = schema
        self.patterns: list[Pattern] = []
        self.values: dict[Variable, set] = defaultdict(set)  # variable -> IRIs a VALUES block binds it to
        self.values_vars: set[Variable] = set()
        self.expressions: list = []
        self._unions = itertools.count()
        self._visit(algebra, ())
        self.types: dict[Variable, list[Pattern]] = defaultdict(list)
        for pattern in self.patterns:
            if pattern.predicate == RDF.type and isinstance(pattern.subject, Variable):
                self.types[pattern.subject].append(pattern)

    def _visit(self, node, branch: tuple) -> None:
        if isinstance(node, (list, tuple, set)):
            for item in node:
                self._visit(item, branch)
            return
        if not isinstance(node, CompValue):
            return
        name = node.name
        if name == "Union":
            union = next(self._unions)
            self._visit(node.p1, branch + ((union, 0),))
            self._visit(node.p2, branch + ((union, 1),))
            return
        if name == "GroupOrUnionGraphPattern" and isinstance(node.graph, list) and len(node.graph) > 1:
            union = next(self._unions)  # a UNION inside EXISTS keeps the parse tree's shape
            for index, alternative in enumerate(node.graph):
                self._visit(alternative, branch + ((union, index),))
            return
        if name == "BGP":
            self.patterns.extend(Pattern(s, p, o, branch) for s, p, o in node.triples)
            return
        if name == "TriplesBlock":  # inside EXISTS: each entry is a flat list of subject, predicate, object terms
            for terms in node.triples:
                if isinstance(terms, (list, tuple)) and len(terms) % 3 == 0:
                    self.patterns.extend(Pattern(*terms[i:i + 3], branch) for i in range(0, len(terms), 3))
            return
        if name == "values":
            for row in node.res:
                for variable, term in row.items():
                    self.values_vars.add(variable)
                    if isinstance(term, URIRef):
                        self.values[variable].add(term)
            return
        if name in ("Filter", "Extend", "LeftJoin") and node.expr is not None:
            self.expressions.append(node.expr)
        for key, value in node.items():
            if not key.startswith("_"):
                self._visit(value, branch)

    def links(self):
        """(pattern, property name) for every pattern whose predicate is an sphn property."""
        for pattern in self.patterns:
            if isinstance(pattern.subject, Variable) and isinstance(pattern.predicate, URIRef):
                predicate = str(pattern.predicate)
                if predicate.startswith(SPHN):
                    yield pattern, predicate[len(SPHN):]

    def single_class(self, subject, anchor: Pattern) -> str | None:
        """The one sphn class the subject is typed with where anchor applies; None when it is untyped,
        typed only by a variable or VALUES, typed with an unknown class, or typed twice (nothing
        matches then). A concrete class decides even when '?x a ?cls' is also present: records carry
        one class, so ?cls can only be that one."""
        names = set()
        for pattern in self.types.get(subject, ()):
            if not pattern.joins(anchor) or isinstance(pattern.object, Variable):
                continue
            name = _class_name(pattern.object, self.schema)
            if name is None:
                return None
            names.add(name)
        return names.pop() if len(names) == 1 else None

    def constants_of(self, variable: Variable) -> set[str]:
        """IRIs the variable is pinned to by VALUES, '=' or IN."""
        found = {str(term) for term in self.values.get(variable, ())}
        for expression in self.expressions:
            _equalities(expression, variable, found)
        return found

    def namespaces_of(self, variable: Variable) -> set[str]:
        """Namespace strings the variable is restricted to with STRSTARTS."""
        found: set[str] = set()
        for expression in self.expressions:
            _namespace_filters(expression, variable, found)
        return found


def _equalities(node, variable: Variable, found: set[str]) -> None:
    if isinstance(node, (list, tuple)):
        for item in node:
            _equalities(item, variable, found)
        return
    if not isinstance(node, CompValue):
        return
    if node.name == "RelationalExpression":
        left, right = node.expr, node.other
        if node.op == "=":
            for mine, other in ((left, right), (right, left)):
                if isinstance(mine, Variable) and mine == variable and isinstance(other, URIRef):
                    found.add(str(other))
        elif node.op == "IN" and isinstance(left, Variable) and left == variable:
            found.update(str(term) for term in (right if isinstance(right, (list, tuple)) else [right]) if isinstance(term, URIRef))
    for key, value in node.items():
        if not key.startswith("_"):
            _equalities(value, variable, found)


def _namespace_filters(node, variable: Variable, found: set[str]) -> None:
    if isinstance(node, (list, tuple)):
        for item in node:
            _namespace_filters(item, variable, found)
        return
    if not isinstance(node, CompValue):
        return
    if node.name == "Builtin_STRSTARTS":
        subject = _variable_of(node.arg1)
        namespace = _string_of(node.arg2)
        if subject is not None and subject == variable and namespace:
            found.add(namespace)
    for key, value in node.items():
        if not key.startswith("_"):
            _namespace_filters(value, variable, found)


def _variable_of(expression) -> Variable | None:
    if isinstance(expression, Variable):
        return expression
    if isinstance(expression, CompValue) and expression.name == "Builtin_STR":
        return _variable_of(expression.arg)
    return None


def _string_of(expression) -> str | None:
    if isinstance(expression, Literal):
        return str(expression)
    if isinstance(expression, URIRef):  # STR(chop:) names the namespace through its prefix
        return str(expression)
    if isinstance(expression, CompValue) and expression.name == "Builtin_STR":
        return _string_of(expression.arg)
    return None


def _class_name(term, schema: SchemaCards) -> str | None:
    if isinstance(term, URIRef) and str(term).startswith(SPHN):
        name = str(term)[len(SPHN):]
        if name in schema.classes:
            return name
    return None


def _properties(schema: SchemaCards) -> set[str]:
    return {prop for card in schema.classes.values() for prop in card["properties"]}


def _individual_properties(schema: SchemaCards) -> set[str]:
    """Properties whose values are sphn-individual entries: the entry is the category itself."""
    return {prop for card in schema.classes.values() for prop, info in card["properties"].items() if info.get("kind") == "individual"}


def _link_targets(prop: str, schema: SchemaCards) -> set[str]:
    """Record classes the property points at, across every card; a 'targets' list on a card lists them all."""
    targets: set[str] = set()
    for card in schema.classes.values():
        info = card["properties"].get(prop)
        if info is None:
            continue
        listed = info.get("targets")
        if isinstance(listed, list) and listed:
            targets.update(str(target) for target in listed)
        elif info.get("target"):
            targets.add(info["target"])
    return targets


def _stem(name: str) -> str:
    """hasAdministrationRouteCode -> AdministrationRoute; hasCode stays Code."""
    stem = name[3:] if name.startswith("has") and len(name) > 3 else name
    if stem.endswith("Code") and len(stem) > 4:
        stem = stem[:-4]
    return stem


def _snake(name: str) -> str:
    return "_".join(words(name)) or "value"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _expand(value: str) -> str:
    """A value-set entry ('snomed:1', '<http://x>') as a full IRI string."""
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    prefix, separator, local = value.partition(":")
    if separator and prefix != "bif" and prefix in STANDARD_PREFIXES:
        return STANDARD_PREFIXES[prefix] + local
    return value


def _short(iri: str) -> str:
    matches = [(len(namespace), prefix) for prefix, namespace in STANDARD_PREFIXES.items() if prefix != "bif" and iri.startswith(namespace)]
    if not matches:
        return f"<{iri}>"
    length, prefix = max(matches)
    return f"{prefix}:{iri[length:]}"


def _system_of(iri: str) -> str | None:
    """The code system an IRI or a namespace string belongs to; None when it names none or several."""
    systems = {
        system for system, namespace in CODE_SYSTEMS.items()
        if iri.startswith(namespace) or (len(iri) >= 12 and namespace.startswith(iri))
    }
    return systems.pop() if len(systems) == 1 else None


def _parent_code(iri: str, allowed: set[str]) -> bool:
    """icd-10-gm:A00 covers icd-10-gm:A00.1 under inference."""
    return iri.startswith(HIERARCHICAL_NAMESPACES) and any(value.startswith(iri + ".") for value in allowed)


def _property_summary(prop: str, info: dict) -> str:
    target = info.get("target")
    if target and target not in CODE_TARGETS:
        return f"sphn:{prop} -> sphn:{target}"
    return f"sphn:{prop}"


def _describe_properties(name: str, schema: SchemaCards) -> str:
    """The properties of a record class as the model should use them."""
    parts = []
    for prop, info in schema.classes[name]["properties"].items():
        if prop in BOILERPLATE:
            continue
        target = info.get("target")
        if info.get("kind") == "individual":
            parts.append(f"{prop} (value set, group by it directly)")
        elif target and target not in CODE_TARGETS:
            parts.append(f"{prop} -> sphn:{target}")
        elif info.get("kind") == "literal":
            parts.append(f"{prop} ({info.get('datatype') or 'value'})")
        else:
            parts.append(f"{prop} (code node: ?r sphn:{prop} ?c . ?c a ?concept)")
    return ", ".join(parts) if parts else "none beyond its identifiers"


def _replacement(prop: str, candidates: list[str]) -> str | None:
    """The one property of the class that the missing one was meant to be, when there is exactly one."""
    stem = _stem(prop)
    same_stem = [candidate for candidate in candidates if _stem(candidate) == stem]
    if len(same_stem) == 1:
        return same_stem[0]
    if same_stem:
        return None
    close = [candidate for candidate in candidates if difflib.SequenceMatcher(None, prop, candidate).ratio() >= REPLACEMENT_RATIO]
    return close[0] if len(close) == 1 else None


def check_class_property(parsed: ParsedQuery, text: str, schema: SchemaCards) -> list[str]:
    """A property that exists in the data, used on a class whose records do not carry it."""
    if schema.empty or parsed.algebra is None:
        return []
    analysis = _Analysis(parsed.algebra, schema)
    known = _properties(schema)
    missing: dict[str, list[str]] = {}
    for pattern, prop in analysis.links():
        if prop not in known:
            continue  # the judge's term check reports properties that occur nowhere
        name = analysis.single_class(pattern.subject, pattern)
        if name is None or prop in schema.classes[name]["properties"]:
            continue
        if prop not in missing.setdefault(name, []):
            missing[name].append(prop)
    errors = []
    for name, props in missing.items():
        card = schema.classes[name]["properties"]
        have = [prop for prop in card if prop not in BOILERPLATE]
        props.sort(key=lambda prop: (text.find(prop) < 0, text.find(prop)))  # as written in the query
        parts = [f"sphn:{name} has no {_join([f'sphn:{prop}' for prop in props])} in the data."]
        for prop in props:
            replacement = _replacement(prop, have)
            if replacement:
                parts.append(f"Write sphn:{replacement} instead of sphn:{prop}.")
        parts.append("Its properties: " + ", ".join(_property_summary(prop, card[prop]) for prop in have) + ".")
        errors.append(" ".join(parts))
    return errors


def check_record_link_typed(parsed: ParsedQuery, text: str, schema: SchemaCards) -> list[str]:
    """'?y a ?cls' on the object of a link to one record class: the only class it yields is that one."""
    if schema.empty or parsed.algebra is None:
        return []
    analysis = _Analysis(parsed.algebra, schema)
    linked: dict[Variable, list[tuple[str, str, Pattern]]] = defaultdict(list)
    for pattern, prop in analysis.links():
        if prop.endswith("Code") or not isinstance(pattern.object, Variable):
            continue
        targets = _link_targets(prop, schema)
        if len(targets) != 1:
            continue  # no target known, or instances of several classes: grouping by the class is legitimate
        target = targets.pop()
        if target in CODE_TARGETS or target not in schema.classes:
            continue
        linked[pattern.object].append((prop, target, pattern))
    errors, seen = [], set()
    for variable, links in linked.items():
        for typed in analysis.types.get(variable, ()):
            cls = typed.object
            if not isinstance(cls, Variable) or cls in analysis.values_vars or (variable, cls) in seen:
                continue
            matches = {(prop, target) for prop, target, link in links if link.joins(typed)}
            if len(matches) != 1:
                continue
            seen.add((variable, cls))
            prop, target = matches.pop()
            errors.append(
                f"?{variable} is a sphn:{target} record (object of {prop}), not a code node: "
                f"'?{variable} a ?{cls}' only yields that class. "
                f"Use its own properties: {_describe_properties(target, schema)}."
            )
    return errors


def _typed_value_sets(analysis: _Analysis, individual: set[str]):
    """(property, value variable, type pattern) for every '?x sphn:P ?v . ?v a ...' with P a value-set property."""
    for pattern, prop in analysis.links():
        if prop not in individual or not isinstance(pattern.object, Variable):
            continue
        for typed in analysis.types.get(pattern.object, ()):
            if typed.joins(pattern):
                yield prop, pattern.object, typed


def check_typed_value_set(parsed: ParsedQuery, text: str, schema: SchemaCards) -> list[str]:
    """'?v a ...' on the object of a value-set property, left after the rewrite could not remove it."""
    if schema.empty or parsed.algebra is None:
        return []
    analysis = _Analysis(parsed.algebra, schema)
    errors, seen = [], set()
    for prop, value, _typed in _typed_value_sets(analysis, _individual_properties(schema)):
        if (prop, value) not in seen:
            seen.add((prop, value))
            errors.append(
                f"sphn:{prop} holds a value-set entry (sphn-individual:...), not a code node: "
                f"drop '?{value} a ...' and group by or compare ?{value} itself."
            )
    return errors


def _variable_re(name: str) -> str:
    return rf"[?$]{re.escape(name)}(?!\w)"


def _delete_type_triple(text: str, value: str, concept: str) -> str | None:
    """Remove '?value a ?concept' from the query text and use ?value wherever ?concept was."""
    value_re, concept_re = _variable_re(value), _variable_re(concept)
    typed = rf"{value_re}\s+{TYPE_RE}\s+{concept_re}{OPTION_RE}"
    forms = (
        (rf"{typed}\s*\.", ""),  # a statement of its own
        (rf"{typed}\s*;\s*", f"?{value} "),  # the head of a ; chain on ?value
        (rf"\s*;\s*{TYPE_RE}\s+{concept_re}{OPTION_RE}", ""),  # a later part of the chain; verified by re-parsing
        (rf"{typed}(?=\s*\}})", ""),  # the last statement of a group, without a dot
    )
    for form, replacement in forms:
        rewritten, count = re.subn(form, replacement, text, count=1)
        if count:
            return re.sub(concept_re, f"?{value}", rewritten)
    return None


def _duplicate_projection(node) -> bool:
    if isinstance(node, (list, tuple, set)):
        return any(_duplicate_projection(item) for item in node)
    if not isinstance(node, CompValue):
        return False
    if node.name == "Project" and len(node.PV) != len(set(node.PV)):
        return True
    return any(_duplicate_projection(value) for key, value in node.items() if not key.startswith("_"))


def _same_patterns(parsed: ParsedQuery, rewritten: str, value: str, concept: str, schema: SchemaCards) -> bool:
    """True when the rewrite removed exactly the type triple and renamed the concept variable, nothing else."""
    reparsed = parse_sparql(rewritten)
    if reparsed.algebra is None or reparsed.kind != parsed.kind or _duplicate_projection(reparsed.algebra):
        return False

    def renamed(term):
        return Variable(value) if isinstance(term, Variable) and term == Variable(concept) else term

    before = Counter(
        (renamed(pattern.subject), renamed(pattern.predicate), renamed(pattern.object))
        for pattern in _Analysis(parsed.algebra, schema).patterns
    )
    before[(Variable(value), RDF.type, Variable(value))] -= 1
    after = Counter((pattern.subject, pattern.predicate, pattern.object) for pattern in _Analysis(reparsed.algebra, schema).patterns)
    return +before == after


def rewrite_typed_value_set(text: str, schema: SchemaCards) -> tuple[str, list[str]]:
    """Delete '?v a ?c' where ?v holds a value-set entry and use ?v wherever ?c was used.

    An sphn-individual IRI is the category itself and carries no rdf:type inside patient graphs, so
    the typed query matches nothing; the entry is a category, which the guard allows in results.
    """
    repairs: list[str] = []
    if schema.empty:
        return text, repairs
    individual = _individual_properties(schema)
    failed: set[tuple[str, str]] = set()
    for _ in range(8):
        parsed = parse_sparql(text)
        if parsed.algebra is None:
            break
        candidates = (
            (prop, str(value), str(typed.object))
            for prop, value, typed in _typed_value_sets(_Analysis(parsed.algebra, schema), individual)
            if isinstance(typed.object, Variable) and (str(value), str(typed.object)) not in failed
        )
        candidate = next(candidates, None)
        if candidate is None:
            break
        prop, value, concept = candidate
        rewritten = _delete_type_triple(text, value, concept)
        if rewritten is not None and _same_patterns(parsed, rewritten, value, concept, schema):
            text = rewritten
            repairs.append(
                f"Removed '?{value} a ?{concept}' and replaced ?{concept} by ?{value}: sphn:{prop} holds a "
                "value-set entry (sphn-individual:...), which is the category itself, not a code node."
            )
        else:
            failed.add((value, concept))
    return text, repairs


def _value_index(schema: SchemaCards) -> dict[str, list[tuple[str, str, str]]]:
    """Full IRI of every value-set entry -> [(class, property, label)]."""
    index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for name, props in schema.value_sets.items():
        for prop, values in props.items():
            for entry in values:
                index[_expand(entry["value"])].append((name, prop, entry.get("label") or ""))
    return index


def _matched_constants(analysis: _Analysis, pattern: Pattern, individual: bool) -> set[str]:
    """IRIs the query matches the property's value against: the entry itself for a value set of
    individuals, the class of the code node for a value set of codes."""
    target = pattern.object
    if individual:
        if isinstance(target, URIRef):
            return {str(target)}
        return analysis.constants_of(target) if isinstance(target, Variable) else set()
    found: set[str] = set()
    if isinstance(target, Variable):
        for typed in analysis.types.get(target, ()):
            if not typed.joins(pattern):
                continue
            if isinstance(typed.object, URIRef):
                found.add(str(typed.object))
            elif isinstance(typed.object, Variable):
                found.update(analysis.constants_of(typed.object))
    return found


def _link_to(name: str, target: str, schema: SchemaCards) -> str | None:
    for prop, info in schema.classes[name]["properties"].items():
        if info.get("target") == target:
            return prop
    return None


def check_value_set_member(parsed: ParsedQuery, text: str, schema: SchemaCards) -> list[str]:
    """Constants matched against a class property that are values of another class's property."""
    if schema.empty or not schema.value_sets or parsed.algebra is None:
        return []
    analysis = _Analysis(parsed.algebra, schema)
    index = _value_index(schema)
    errors, seen = [], set()
    for pattern, prop in analysis.links():
        name = analysis.single_class(pattern.subject, pattern)
        if name is None:
            continue
        values = schema.value_sets.get(name, {}).get(prop)
        if not values or (name, prop) in seen:
            continue
        allowed = {_expand(entry["value"]) for entry in values}
        individual = all(value.startswith(STANDARD_PREFIXES["sphn-individual"]) for value in allowed)
        constants = _matched_constants(analysis, pattern, individual)
        bad = sorted(iri for iri in constants if iri not in allowed and not iri.startswith(SPHN) and not _parent_code(iri, allowed))
        homes: Counter = Counter()
        labels: dict[str, str] = {}
        for iri in bad:
            for home_name, home_prop, label in index.get(iri, ()):
                if (home_name, home_prop) != (name, prop):
                    homes[(home_name, home_prop)] += 1
                    labels.setdefault(iri, label)
        if not homes:
            continue  # unknown codes may be hierarchy parents or inference roots: stay silent
        seen.add((name, prop))
        (home_name, home_prop), _count = min(homes.items(), key=lambda item: (-item[1], _link_to(name, item[0][0], schema) is None, item[0]))
        shown = [f"{_short(iri)} ({labels[iri]})" if labels.get(iri) else _short(iri) for iri in bad]
        own = " | ".join(f"{entry['label']} {entry['value']}" for entry in values)
        subject, code = pattern.subject, pattern.object if isinstance(pattern.object, Variable) else "code"
        if individual:
            pattern_text = f"?{subject} a sphn:{home_name} ; sphn:{home_prop} {_short(bad[0])}"
        elif _link_to(name, home_name, schema):
            via = _snake(home_name)
            pattern_text = f"?{subject} sphn:{_link_to(name, home_name, schema)} ?{via} . ?{via} sphn:{home_prop} ?{code} . ?{code} a ?concept"
        else:
            pattern_text = f"?{_snake(home_name)} a sphn:{home_name} ; sphn:{home_prop} ?{code} . ?{code} a ?concept"
        one = len(bad) == 1
        errors.append(
            f"{_join(shown)} {'is not a value' if one else 'are not values'} of sphn:{name} {prop} (its values: {own}). "
            f"{'It is a value' if one else 'They are values'} of sphn:{home_name} {home_prop}: write {pattern_text}."
        )
    return errors


def _systems_assumed(analysis: _Analysis, pattern: Pattern) -> set[str]:
    """Code systems the query assumes for the object of a code property."""
    target = pattern.object
    found: set[str | None] = set()
    if isinstance(target, URIRef):
        found.add(_system_of(str(target)))
    elif isinstance(target, Variable):
        found.update(_system_of(namespace) for namespace in analysis.namespaces_of(target))
        for typed in analysis.types.get(target, ()):
            if not typed.joins(pattern):
                continue
            if isinstance(typed.object, URIRef):
                found.add(_system_of(str(typed.object)))
            elif isinstance(typed.object, Variable):
                found.update(_system_of(iri) for iri in analysis.constants_of(typed.object))
                found.update(_system_of(namespace) for namespace in analysis.namespaces_of(typed.object))
    found.discard(None)
    return found


def check_code_system(parsed: ParsedQuery, text: str, schema: SchemaCards) -> list[str]:
    """A typed code pattern or a namespace filter of one code system on a class whose codes belong
    to another, when the card says which system the class carries ("code_system")."""
    if schema.empty or parsed.algebra is None:
        return []
    analysis = _Analysis(parsed.algebra, schema)
    errors, seen = [], set()
    for pattern, prop in analysis.links():
        name = analysis.single_class(pattern.subject, pattern)
        if name is None:
            continue
        system = schema.classes[name]["properties"].get(prop, {}).get("code_system")
        if system != "generic" and system not in CODE_SYSTEMS:
            continue  # absent or unknown: nothing to say
        wrong = sorted(assumed for assumed in _systems_assumed(analysis, pattern) if assumed != system)
        if not wrong or (name, prop) in seen:
            continue
        seen.add((name, prop))
        if system != "generic":
            errors.append(f"sphn:{name} codes are {system} ({CODE_SYSTEMS[system]}), not {' or '.join(wrong)}.")
            continue
        for assumed in wrong:
            carriers = sorted(
                other for other, card in schema.classes.items()
                if card["properties"].get(prop, {}).get("code_system") == assumed
            )
            carried = (
                f"{assumed} codes are on {_join([f'sphn:{other}' for other in carriers])}" if carriers
                else f"no class carries {assumed} codes on sphn:{prop}"
            )
            errors.append(f"sphn:{name} codes are generic sphn:Code nodes in the data; {carried}.")
    return errors


def _classes_named(stem: str, schema: SchemaCards) -> list[str]:
    """The class the stem names (Age) and the largest class whose name ends with it (AdministrativeSex)."""
    if len(stem) < 3:
        return []
    target = words(stem)
    exact = [name for name in schema.classes if name == stem]
    suffix = [name for name in schema.classes if name != stem and words(name)[-len(target):] == target]
    suffix.sort(key=lambda name: (-schema.classes[name]["patients"], name))
    return (exact + suffix)[:2]


def _record_pattern(stem: str, classes: list[str], schema: SchemaCards) -> str:
    main, *others = classes
    card = schema.classes[main]["properties"]
    also = f" (also {_join([f'sphn:{other}' for other in others])})" if others else ""
    code = "hasCode" if "hasCode" in card else next((prop for prop in card if prop.endswith("Code") and prop not in BOILERPLATE), None)
    if code:
        return f"{stem} is a record: ?s a sphn:{main} ; sphn:{code} ?c . ?c a ?concept{also}. Never return ?c."
    own = [prop for prop in card if prop not in BOILERPLATE]
    # The property that carries the value comes first: a linked record (Age -> Quantity), else a value set, else a literal.
    rank = {"resource": 0, "individual": 1}
    carrier = min(own, key=lambda prop: (rank.get(card[prop].get("kind"), 2), own.index(prop)), default=None)
    pattern = f"?s a sphn:{main}"
    if carrier:
        variable = _snake(_stem(carrier))
        pattern += f" ; sphn:{carrier} ?{variable}"
        target = card[carrier].get("target")
        if target in schema.classes and "hasValue" in schema.classes[target]["properties"]:
            pattern += f" . ?{variable} sphn:hasValue ?value"
    listing = ", ".join(_property_summary(prop, card[prop]) for prop in own) or "none beyond its identifiers"
    return f"{stem} is a record: {pattern}{also}. Its properties: {listing}."


def _close_term(term: str, schema: SchemaCards) -> str | None:
    """A property with the same stem (hasRankCode -> hasRank) or a near-identical data term."""
    stem = _stem(term)
    same_stem = sorted(candidate for candidate in schema.data_terms if candidate != term and candidate.startswith("has") and _stem(candidate) == stem)
    if len(same_stem) == 1:
        return same_stem[0]
    scored = [(difflib.SequenceMatcher(None, term, candidate).ratio(), candidate) for candidate in schema.data_terms]
    best = max(scored, default=None)
    return best[1] if best and best[0] >= SUGGESTION_RATIO else None


def suggest_missing_term(term: str, schema: SchemaCards) -> str:
    """The judge's message for an sphn term absent from the data: the record pattern of the class the
    term's stem names (hasSex -> AdministrativeSex), else a property only when the names nearly coincide."""
    message = f"sphn:{term} does not occur in the data."
    if schema.empty:
        return message
    classes = _classes_named(_stem(term), schema)
    if classes:
        return f"{message} {_record_pattern(_stem(term), classes, schema)}"
    suggestion = _close_term(term, schema)
    return f"{message} Did you mean sphn:{suggestion}?" if suggestion else message


CHECKS = [check_class_property, check_record_link_typed, check_typed_value_set, check_value_set_member, check_code_system]
REWRITES = [rewrite_typed_value_set]
