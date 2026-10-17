"""Prototype of three class-aware judge checks, run over examples, dev references and dev generated queries.

Nothing here touches the repo; holdout rows are filtered out before anything is read or printed.
"""
import json
import re
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/santiagofigueroa/Desktop/Hackathon/nl2sparql/src")
from rdflib import URIRef, Variable  # noqa: E402
from rdflib.namespace import RDF  # noqa: E402
from rdflib.plugins.sparql.parserutils import CompValue  # noqa: E402

from nl2sparql.sparql.parse import parse_sparql  # noqa: E402
from nl2sparql.sparql.prefixes import add_missing_prefixes  # noqa: E402

REPO = "/Users/santiagofigueroa/Desktop/Hackathon/nl2sparql"
SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
INDIVIDUAL = "https://biomedit.ch/rdf/sphn-schema/sphn/individual#"
cards = json.load(open(f"{REPO}/data/schema_cards.json"))["classes"]
kinds = json.load(open(f"{REPO}/data/property_kinds.json"))
value_sets = json.load(open(f"{REPO}/assets/value_sets.json"))["classes"]
PREFIX_OF = {"http://snomed.info/id/": "snomed:", "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/": "icd-10-gm:",
             "https://biomedit.ch/rdf/sphn-resource/chop/": "chop:", INDIVIDUAL: "sphn-individual:"}


def short(iri: str) -> str:
    for ns, p in PREFIX_OF.items():
        if iri.startswith(ns):
            return p + iri[len(ns):]
    return iri


def triples(algebra):
    found = []

    def visit(node):
        if isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "BGP":
            found.extend(node.triples)
            return
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value)

    visit(algebra)
    return found


def checks(sparql: str) -> list[str]:
    parsed = parse_sparql(add_missing_prefixes(sparql.strip() + "\n"))
    if parsed.query is None:
        return [f"(unparsed: {parsed.error})"] if parsed.kind == "Invalid" else []
    ts = triples(parsed.algebra)
    classes_of = defaultdict(set)   # subject var -> explicit sphn classes
    props_of = defaultdict(list)    # subject var -> [(prop name, object)]
    typed_as = defaultdict(set)     # var -> rdf:type objects (IRIs or vars)
    for s, p, o in ts:
        if p == RDF.type and isinstance(s, Variable):
            typed_as[s].add(o)
            if isinstance(o, URIRef) and str(o).startswith(SPHN) and str(o)[len(SPHN):] in cards:
                classes_of[s].add(str(o)[len(SPHN):])
        elif isinstance(p, URIRef) and str(p).startswith(SPHN) and isinstance(s, Variable):
            props_of[s].append((str(p)[len(SPHN):], o))
    errors = []
    # 1. class-scoped property: the property is not on the card of the subject's explicit class
    for s, names in classes_of.items():
        for prop, o in props_of[s]:
            for cls in names:
                if prop not in cards[cls]["properties"] and prop in {q for c in cards.values() for q in c["properties"]}:
                    have = ", ".join(f"sphn:{q}" for q in cards[cls]["properties"] if q not in ("hasDataProvider", "hasSourceSystem", "hasSubjectPseudoIdentifier"))
                    errors.append(f"[A] sphn:{cls} has no sphn:{prop} in the data. Its properties: {have}.")
    # 2. a value-set object (kind individual) given an rdf:type pattern
    for s, plist in props_of.items():
        for prop, o in plist:
            info = kinds.get(SPHN + prop)
            if info and info["kind"] == "individual" and isinstance(o, Variable) and o in typed_as:
                errors.append(f"[B] sphn:{prop} holds a value-set entry (sphn-individual:...), not a code node: "
                              f"drop '?{o} a ...' and group by or compare ?{o} itself.")
    # 3. value-set member on the wrong class: ?x a C ; P ?c . ?c a <iri> (or ?c a ?t constrained by constants)
    text = parsed.text
    for s, names in classes_of.items():
        for prop, o in props_of[s]:
            for cls in names:
                allowed = value_sets.get(cls, {}).get(prop)
                if not allowed or not isinstance(o, Variable):
                    continue
                allowed_iris = {v["value"] for v in allowed}
                allowed_full = set()
                for v in allowed_iris:
                    for ns, p in PREFIX_OF.items():
                        if v.startswith(p):
                            allowed_full.add(ns + v[len(p):])
                constants = set()
                for t in typed_as.get(o, ()):
                    if isinstance(t, URIRef):
                        constants.add(str(t))
                    elif isinstance(t, Variable):  # ?c a ?t : constants compared with ?t in FILTER / VALUES / IN
                        for m in re.finditer(rf"\?{re.escape(str(t))}\s*(?:=|IN\s*\()\s*([^)\n&|]+)", text):
                            constants.update(_iris(m.group(1), text))
                        for m in re.finditer(rf"VALUES\s*\(\s*\?{re.escape(str(t))}[^{{]*\{{([^}}]*)\}}", text):
                            constants.update(_iris(m.group(1), text))
                        for m in re.finditer(rf"VALUES\s+\?{re.escape(str(t))}\s*\{{([^}}]*)\}}", text):
                            constants.update(_iris(m.group(1), text))
                bad = [c for c in constants if c not in allowed_full and c not in allowed_iris and not c.startswith(SPHN)]
                if bad:
                    where = [f"sphn:{c2}.{p2}" for c2, props in value_sets.items() for p2, vals in props.items()
                             if any(v["value"] in {short(b) for b in bad} for v in vals)]
                    errors.append(f"[C] {', '.join(short(b) for b in bad)} is not a value of sphn:{cls} {prop} "
                                  f"(its values: {' | '.join(v['label'] + ' ' + v['value'] for v in allowed)}). "
                                  f"It is a value of {', '.join(where) or 'no card'}.")
    return errors


def _iris(fragment: str, text: str) -> set[str]:
    prefixes = dict(re.findall(r"PREFIX\s+([\w-]*):\s*<([^>]+)>", text))
    out = set()
    for m in re.finditer(r"<([^>]+)>|([\w-]*):([\w.]+)", fragment):
        if m.group(1):
            out.add(m.group(1))
        elif m.group(2) in prefixes:
            out.add(prefixes[m.group(2)] + m.group(3))
    return out


def main():
    sets = []
    examples = [json.loads(l) for l in open(f"{REPO}/data/examples.jsonl") if l.strip()]
    sets.append(("official examples", [(e["id"], e["sparql"]) for e in examples]))
    refs = [json.loads(l) for l in open(f"{REPO}/eval/query_questions.jsonl") if l.strip()]
    refs = [r for r in refs if r.get("split", "dev") != "holdout"]
    sets.append(("dev reference queries", [(r["id"], r["reference_sparql"]) for r in refs]))
    report = json.load(open(f"{REPO}/eval/reports/query-20261017-140904.json"))
    gen = [r for r in report["rows"] if r.get("split") != "holdout" and r.get("sparql_query")]
    sets.append(("dev generated queries", [(f"{r['id']} [{r['outcome']}]", r["sparql_query"]) for r in gen]))
    for name, items in sets:
        print("=" * 30, name, len(items))
        flagged = 0
        for ident, sparql in items:
            errs = checks(sparql)
            if errs:
                flagged += 1
                print(f"-- {ident}")
                for e in errs:
                    print("     ", e[:260])
        print(f"   flagged {flagged}/{len(items)}")


if __name__ == "__main__":
    main()
