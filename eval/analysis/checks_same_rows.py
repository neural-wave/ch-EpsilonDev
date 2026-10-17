"""Prototype judge checks for the same_rows failure cluster, measured on several corpora.

Corpora: the 13 generated queries (expected hits), their references + official examples + the dev
reference bank (expected ~0 hits = false positives), and the guard corpus (LLM-written queries).
Holdout rows of eval/query_questions.jsonl are skipped unread.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/Users/santiagofigueroa/Desktop/Hackathon/nl2sparql/src")
from rdflib import Literal, URIRef, Variable  # noqa: E402
from rdflib.namespace import RDF  # noqa: E402
from rdflib.plugins.sparql.parserutils import CompValue  # noqa: E402

from nl2sparql.sparql.parse import parse_sparql  # noqa: E402
from nl2sparql.sparql.prefixes import add_missing_prefixes  # noqa: E402

REPO = Path("/Users/santiagofigueroa/Desktop/Hackathon/nl2sparql")
BASE = Path(__file__).resolve().parent / "same-rows"  # per-case {generated,reference}.rq files, when present
CODE_ROOTS = ("https://biomedit.ch/rdf/sphn-resource/icd-10-gm/", "https://biomedit.ch/rdf/sphn-resource/chop/")
TERMINOLOGY_ROOTS = CODE_ROOTS + ("http://snomed.info/id/", "https://loinc.org/rdf/", "https://biomedit.ch/rdf/sphn-resource/ucum/",
                                  "http://purl.obolibrary.org/obo/")
PATIENT_WORDS = ("patient", "subject", "people", "person", "individual")
RECORD_WORDS = ("record", "episode", "event", "stay", "case", "prescription", "diagnos", "procedure", "test", "visit",
                "admission", "administration", "intake", "sample", "measurement", "follow",
                "allerg", "consent", "variant", "surger", "radiother", "drug", "lab", "report", "entries", "entry")  # birth/death left out: "births" = babies


def walk(node, fn, in_subquery=False):
    """Visit every CompValue; fn(node) may return True to stop descending."""
    if isinstance(node, (list, tuple, set)):
        for item in node:
            walk(item, fn, in_subquery)
        return
    if not isinstance(node, CompValue):
        return
    if fn(node):
        return
    for key, value in node.items():
        if not key.startswith("_"):
            walk(value, fn, in_subquery)


def expr_vars(expr) -> set:
    found = set()

    def visit(n):
        if isinstance(n, Variable):
            found.add(n)
        elif isinstance(n, (list, tuple, set)):
            for i in n:
                visit(i)
        elif isinstance(n, CompValue):
            for k, v in n.items():
                if not k.startswith("_"):
                    visit(v)

    visit(expr)
    return found


def bgp_triples(node) -> list:
    triples = []

    def fn(n):
        if n.name == "BGP":
            triples.extend(n.triples)
        return False

    walk(node, fn)
    return triples


def aggregate_joins(algebra) -> list:
    found = []
    walk(algebra, lambda n: found.append(n) or False if n.name == "AggregateJoin" else False)
    return found


def column_aggregates(algebra) -> list[tuple[str, CompValue]]:
    """(alias, aggregate) for every column whose expression is directly one aggregate, scoped per
    SELECT (rdflib reuses __agg_N__ names across subqueries)."""
    cols = []

    def scope(project):
        extends, aggs = [], {}

        def down(n):
            if not isinstance(n, CompValue):
                return
            if n.name == "Project" and n is not project:
                return  # a subquery: its own scope
            if n.name == "Extend":
                extends.append(n)
            if n.name == "AggregateJoin":
                for a in n.A:
                    aggs[a.res] = a
                return  # nothing below the grouping belongs to this SELECT's columns
            for k, v in n.items():
                if not k.startswith("_"):
                    if isinstance(v, (list, tuple)):
                        for i in v:
                            down(i)
                    else:
                        down(v)

        down(project.p)
        for e in extends:
            if isinstance(e.expr, Variable) and e.expr in aggs:
                cols.append((str(e["var"]), aggs[e.expr]))
        return False

    walk(algebra, lambda n: scope(n) if n.name == "Project" else False)
    return cols


def agg_key(a) -> tuple:
    v = a.vars
    v = str(v) if not isinstance(v, CompValue) else repr(v)
    return (a.name, bool(a.distinct), v)


# ---------------------------------------------------------------- checks
def check_unbound_aggregate(parsed) -> list[str]:
    hits = []
    for aj in aggregate_joins(parsed.algebra):
        bound = set(aj.p.p._vars) if hasattr(aj.p, "p") else set()
        for a in aj.A:
            if a.name == "Aggregate_Sample":
                continue
            for v in expr_vars(a.vars):
                if v not in bound:
                    hits.append(f"{a.name.replace('Aggregate_', '').upper()}(?{v}) but ?{v} is never bound")
    return hits


def check_duplicate_column_aggregate(parsed) -> list[str]:
    seen = defaultdict(list)
    for alias, a in column_aggregates(parsed.algebra):
        seen[agg_key(a)].append(alias)
    return [f"columns {aliases} are the same aggregate {key[0]}({'DISTINCT ' if key[1] else ''}?{key[2]})"
            for key, aliases in seen.items() if len(aliases) > 1]


def check_g_count_alias(parsed, variant="patient_words") -> list[str]:
    hits = []
    for alias, a in column_aggregates(parsed.algebra):
        if a.name == "Aggregate_Count" and a.vars == Variable("g"):
            low = alias.lower()
            if variant == "patient_words":
                if not any(w in low for w in PATIENT_WORDS) and low not in ("g", "n", "count", "total"):
                    hits.append(f"COUNT(DISTINCT ?g) counts patients but is named ?{alias}")
            else:
                if not any(w in low for w in PATIENT_WORDS) and any(w in low for w in RECORD_WORDS):
                    hits.append(f"COUNT(DISTINCT ?g) counts patients but is named ?{alias}")
    return hits


def code_node_vars(parsed) -> set:
    out = set()
    for s, p, o in bgp_triples(parsed.algebra):
        if isinstance(p, URIRef) and (str(p).endswith("#hasCode") or str(p).endswith("Code")) and isinstance(o, Variable):
            out.add(o)
    return out


def check_code_node_equality(parsed) -> list[str]:
    nodes = code_node_vars(parsed)
    hits = []

    def fn(n):
        if n.name == "RelationalExpression" and n.op in ("=", "!=", "IN", "NOT IN"):
            sides = [n.expr] + (list(n.other) if isinstance(n.other, list) else [n.other])
            vs = [s for s in sides if isinstance(s, Variable) and s in nodes]
            iris = [s for s in sides if isinstance(s, URIRef) and str(s).startswith(TERMINOLOGY_ROOTS)]
            if vs and iris:
                hits.append(f"?{vs[0]} is a code node, compared with {iris[0]}")
        return False

    walk(parsed.algebra, fn)
    return hits


def check_date_string_arithmetic(parsed) -> list[str]:
    date_vars = {o for s, p, o in bgp_triples(parsed.algebra)
                 if isinstance(p, URIRef) and re.search(r"Date(Time)?$", str(p)) and isinstance(o, Variable)}
    hits = []

    def fn(n):
        if n.name == "AdditiveExpression":
            operands = [n.expr] + list(n.other)
            lits = [x for x in operands if isinstance(x, Literal)]
            dvars = [x for x in operands if isinstance(x, Variable) and x in date_vars]
            if dvars and lits:
                hits.append(f"?{dvars[0]} {n.op[0]} {lits[0]!r}: date arithmetic with a literal")
            elif any(isinstance(x, Literal) and re.match(r"^P\d", str(x)) for x in lits):
                hits.append(f"duration literal {lits[0]!r} in arithmetic")
        return False

    walk(parsed.algebra, fn)
    return hits


def check_values_code_without_inference(parsed) -> list[str]:
    code_vars = set()

    def fn(n):
        if n.name == "values":
            for row in n.res:
                for v, val in row.items():
                    if isinstance(val, URIRef) and str(val).startswith(CODE_ROOTS):
                        code_vars.add(v)
        return False

    walk(parsed.algebra, fn)
    hits = []
    for s, p, o in bgp_triples(parsed.algebra):
        if p == RDF.type and isinstance(o, Variable) and o in code_vars:
            pat = re.compile(rf"\?{re.escape(str(s))}\s+(?:a|rdf:type)\s+\?{re.escape(str(o))}\s+OPTION\s*\(\s*inference", re.I)
            if not pat.search(parsed.text):
                hits.append(f"?{s} a ?{o} matches ICD-10-GM/CHOP codes from VALUES without OPTION (inference)")
    return hits


def check_optional_cross_product(parsed) -> list[str]:
    hits = []
    for aj in aggregate_joins(parsed.algebra):
        agg_vars = set()
        for a in aj.A:
            if a.name in ("Aggregate_Avg", "Aggregate_Sum") or (a.name == "Aggregate_Count" and not a.distinct):
                agg_vars |= expr_vars(a.vars)
        if not agg_vars:
            continue
        optional_bound = []

        def fn(n):
            if n.name in ("Project",):  # do not descend into subqueries
                return True
            if n.name == "LeftJoin":
                new = set(n.p2._vars) - set(n.p1._vars)
                optional_bound.append(new)
            return False

        walk(aj.p.p, fn)
        touching = [b & agg_vars for b in optional_bound if b & agg_vars]
        if len(touching) >= 2:
            hits.append(f"AVG/SUM over variables from {len(touching)} independent OPTIONAL blocks: {[sorted(str(v) for v in t) for t in touching]}")
    return hits


def check_filter_restricts_average(parsed) -> list[str]:
    hits = []
    for aj in aggregate_joins(parsed.algebra):
        stat_vars = set()
        for a in aj.A:
            if a.name in ("Aggregate_Avg", "Aggregate_Max", "Aggregate_Min"):
                stat_vars |= expr_vars(a.vars)
        if not stat_vars:
            continue

        def fn(n):
            if n.name == "Project":
                return True
            if n.name == "Filter" and n.expr.name == "RelationalExpression" and n.expr.op in (">", "<", ">=", "<="):
                vs = expr_vars(n.expr) & stat_vars
                if vs and isinstance(n.expr.other, Literal):
                    hits.append(f"FILTER on ?{sorted(vs)[0]} also restricts AVG/MAX/MIN of it")
            return False

        walk(aj.p.p, fn)
    return hits


def check_round_avg_integer(parsed) -> list[str]:
    avg_res = {a.res for aj in aggregate_joins(parsed.algebra) for a in aj.A if a.name == "Aggregate_Avg"}
    hits = []
    walk(parsed.algebra, lambda n: hits.append("ROUND(AVG(...)) rounds the average to an integer") or False
         if n.name == "Builtin_ROUND" and isinstance(n.arg, Variable) and n.arg in avg_res else False)
    return hits


def check_rebind(parsed) -> list[str]:
    hits = []

    def fn(n):
        if n.name == "Extend" and n["var"] in set(n.p._vars) and not str(n["var"]).startswith("__agg"):
            # rdflib puts projected GROUP BY keys through SAMPLE -> Extend of the same name; skip those
            if not (isinstance(n.expr, Variable) and str(n.expr).startswith("__agg")):
                hits.append(f"BIND reassigns ?{n['var']} which is already bound")
        return False

    walk(parsed.algebra, fn)
    return hits


def check_fake_snomed(parsed) -> list[str]:
    from nl2sparql.sparql.terms import collect_iris
    return [f"{iri} is not a SNOMED CT concept id (numeric)" for iri in sorted(collect_iris(parsed.algebra))
            if str(iri).startswith("http://snomed.info/id/") and not str(iri)[len("http://snomed.info/id/"):].isdigit()]


CHECKS = {
    "fake_snomed_iri": check_fake_snomed,
    "unbound_aggregate": check_unbound_aggregate,
    "duplicate_column_aggregate": check_duplicate_column_aggregate,
    "g_count_alias(patient_words)": lambda p: check_g_count_alias(p, "patient_words"),
    "g_count_alias(record_words)": lambda p: check_g_count_alias(p, "record_words"),
    "code_node_equality": check_code_node_equality,
    "date_string_arithmetic": check_date_string_arithmetic,
    "values_code_without_inference": check_values_code_without_inference,
    "optional_cross_product": check_optional_cross_product,
    "filter_restricts_average": check_filter_restricts_average,
    "round_avg_integer(info)": check_round_avg_integer,
    "rebind_bound_variable": check_rebind,
}


def corpora():
    ids = sorted(p.name for p in BASE.iterdir() if p.is_dir())
    yield "generated", [(cid, (BASE / cid / "generated.rq").read_text()) for cid in ids]
    yield "reference", [(cid, (BASE / cid / "reference.rq").read_text()) for cid in ids]
    ex = [json.loads(l) for l in (REPO / "data/examples.jsonl").read_text().splitlines() if l.strip()]
    yield "official_examples", [(r["id"], r["sparql"]) for r in ex]
    dev = []
    for line in (REPO / "eval/query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("split") == "holdout":
            continue  # blind exam: never read
        dev.append((r["id"], r["reference_sparql"]))
    yield "dev_reference_bank", dev
    gc = [json.loads(l) for l in (REPO / "data/guard_corpus.jsonl").read_text().splitlines() if l.strip()]
    key = "sparql" if "sparql" in gc[0] else [k for k in gc[0] if "sparql" in k.lower()][0]
    yield "guard_corpus(LLM-written)", [(r.get("id", str(i)), r[key]) for i, r in enumerate(gc)]


def main():
    for name, items in corpora():
        print("=" * 100)
        print(f"CORPUS {name}: {len(items)} queries")
        totals = defaultdict(list)
        for cid, text in items:
            parsed = parse_sparql(add_missing_prefixes(text.strip() + "\n"))
            if parsed.kind != "SelectQuery":
                print(f"  {cid}: {parsed.kind} {parsed.error or ''}")
                continue
            for cname, fn in CHECKS.items():
                try:
                    hits = fn(parsed)
                except Exception as e:  # noqa
                    hits = [f"CHECK ERROR {e!r}"]
                if hits:
                    totals[cname].append(cid)
                    print(f"  {cid} [{cname}] {hits[0]}" + (f" (+{len(hits)-1})" if len(hits) > 1 else ""))
        print("  -- hits per check:", {k: len(v) for k, v in totals.items()})


if __name__ == "__main__":
    main()
