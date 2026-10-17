"""Demo-only semantics and release policy over complete executed results."""
from decimal import Decimal, InvalidOperation

from rdflib import Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.sparql.parse import parse_sparql

ROW_LIMIT = 200
POLICY_VERSION = 1


def _walk(node):
    if not isinstance(node, CompValue):
        return
    # Nested SELECTs and EXISTS have separate bindings and aggregate namespaces.
    if node.name in {"ToMultiSet", "Builtin_EXISTS", "Builtin_NOTEXISTS"}:
        return
    for key, value in node.items():
        if key.startswith("_"):
            continue
        for child in value if isinstance(value, (list, tuple)) else [value]:
            yield from _walk(child)
    yield node


def column_metadata(query, columns):
    parsed = parse_sparql(query)
    nodes = list(_walk(parsed.algebra))
    project = next((n for n in reversed(nodes) if n.name == "Project"), None)
    projected = set(project.PV) if project is not None else set()
    aliases, aggregates, groups = {}, {}, set()
    graph_vars, ambiguous = set(), set()
    for node in nodes:
        if node.name == "Extend":
            aliases[node.var] = node.expr
        elif node.name == "AggregateJoin":
            for agg in node.A:
                aggregates[agg.res] = agg
        elif node.name == "Group":
            groups.update(x for x in (node.expr or []) if isinstance(x, Variable))
        elif node.name == "Graph" and isinstance(node.term, Variable):
            triples = [t for n in _walk(node.p) if n.name == "BGP" for t in n.triples]
            # A variable graph with concrete clinical patterns is a patient graph in this dataset.
            clinical = any(
                str(predicate).startswith("https://biomedit.ch/rdf/sphn-schema/sphn#")
                or predicate == RDF.type and str(obj).startswith("https://biomedit.ch/rdf/sphn-schema/sphn#")
                for _, predicate, obj in triples
            )
            (graph_vars if clinical else ambiguous).add(node.term)
    graph_vars -= ambiguous

    def resolve(value):
        seen = set()
        while isinstance(value, Variable) and value in aliases:
            if value in seen:
                return None
            seen.add(value)
            value = aliases[value]
        return value

    result = []
    for name in columns:
        role, kind = "unknown", "unknown"
        variable = Variable(name)
        value = resolve(variable) if variable in projected else None
        agg = aggregates.get(value) if isinstance(value, Variable) else None
        if agg is not None:
            arg = agg.vars
            if agg.name == "Aggregate_Count" and agg.distinct and isinstance(arg, Variable) and arg in graph_vars:
                role, kind = "patient_count", "distinct_patient_count"
            elif agg.name == "Aggregate_Sample" and isinstance(arg, Variable) and arg in groups:
                role, kind = "category", "group_key"
            elif agg.name in {"Aggregate_Count", "Aggregate_Sum", "Aggregate_Avg", "Aggregate_Min", "Aggregate_Max"}:
                role, kind = "numeric_measure", agg.name.removeprefix("Aggregate_").lower()
        elif isinstance(value, Variable) and value in groups:
            role, kind = "category", "group_key"
        result.append({"name": name, "role": role, "kind": kind})
    return result


def process_results(
    query: str,
    columns: list[str],
    rows: list[dict[str, str]],
    threshold: int | None,
) -> dict:
    """Return only released rows (capped after policy checks), or a withheld envelope."""
    meta = column_metadata(query, columns)
    reason = None
    if threshold is not None:
        counts = [m["name"] for m in meta if m["role"] == "patient_count"]
        if not counts or any(m["role"] not in {"patient_count", "category"} for m in meta):
            reason = "The minimum group policy cannot verify this aggregate or derived measure."
        else:
            for row in rows:
                for name in counts:
                    try:
                        value = Decimal(str(row.get(name, "")))
                        if not value.is_finite() or value < 0 or value != value.to_integral_value():
                            raise ValueError
                    except (InvalidOperation, ValueError):
                        reason = "The minimum group policy cannot verify a patient count."
                        break
                    if value < threshold:
                        reason = "A group is below the minimum size and the entire result is hidden."
                        break
                if reason:
                    break
    suppression = None if threshold is None else {
        "enabled": True, "threshold": threshold, "policy_version": POLICY_VERSION,
        "status": "withheld" if reason else "released", "reason": reason,
    }
    if reason:
        return dict(result_status="withheld", rows=None, columns=None, column_metadata=[], suppression=suppression)
    return dict(result_status="released", rows=[{name: row.get(name) for name in columns} for row in rows[:ROW_LIMIT]], columns=columns,
                column_metadata=meta, suppression=suppression,
                truncated=len(rows) > ROW_LIMIT, row_limit=ROW_LIMIT)
