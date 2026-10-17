"""Proposed judge addition: when rdflib cannot parse the query, explain the structural mistake in words the
model can act on, and repair the three mechanical ones (GROUP BY <constant>, GRAPH ... UNION without braces,
GROUP BY written inside the WHERE braces) before giving up.

Runs only on queries rdflib rejected, so it cannot change the verdict of a valid query. Prototype: not in
the repo. `diagnose(text)` -> list of hints; `normalize(text)` -> (text, list of repairs applied).
"""
from __future__ import annotations

import re

STRINGS_AND_IRIS = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|#[^\n]*")
MODIFIER_START = re.compile(r"^\s*(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET|VALUES|\}|$)", re.IGNORECASE)
HINTS = {
    "parens": "Parentheses are not balanced: {opened} '(' but {closed} ')'. In BIND(IF(..., IF(..., ...))) close "
              "every IF and then the BIND: ... , \"90+\")) AS ?x). Simpler: BIND(10 * FLOOR(?age / 10) AS ?decade).",
    "group_by_inside": "GROUP BY is written inside the braces of WHERE. Close WHERE first: WHERE { ... } GROUP BY ?x "
                       "ORDER BY ... (GROUP BY, HAVING, ORDER BY and LIMIT come after the closing brace).",
    "group_by_constant": "GROUP BY {value} is not valid. For one overall row leave GROUP BY out; otherwise group by a variable.",
    "graph_union": "Each UNION branch must be a braced group inside WHERE: WHERE { { GRAPH ?g { ... } } UNION { GRAPH ?g { ... } } }.",
    "union_outside": "UNION appears outside the WHERE braces. Put both branches inside WHERE { { ... } UNION { ... } }.",
    "aggregate_filter": "SPARQL has no FILTER clause after an aggregate (COUNT(...) FILTER (...) is SQL). Count a subgroup with "
                        "OPTIONAL { GRAPH ?g { <condition> } BIND(?g AS ?x) } and COUNT(DISTINCT ?x), or SUM(IF(<condition>, 1, 0)).",
    "window": "Window functions (OVER) do not exist in SPARQL. Compute the total in its own subquery "
              "{ SELECT (COUNT(DISTINCT ?g) AS ?total) WHERE { ... } } and divide by ?total.",
    "type_in_expression": "'a' (rdf:type) is a triple pattern, not an expression: FILTER(?x a <class>) and IF(?x a <class>, ...) are "
                          "invalid. Write ?x a <class> . as a pattern, or ?x a ?concept . VALUES (?concept ?label) { (<A> \"a\") (<B> \"b\") }.",
    "class_alternatives": "Class alternatives cannot be written as ?x a <A> | <B>. Write ?x a ?concept . VALUES (?concept ?label) "
                          "{ (<A> \"a\") (<B> \"b\") } and GROUP BY ?label.",
    "subselect_not_alone": "A subquery { SELECT ... WHERE { ... } } must stand alone inside its braces. Move the BIND/FILTER "
                           "after its closing brace: { SELECT ... } BIND(...).",
    "member_access": "?x.sphn:property is not SPARQL. Bind the value with a triple pattern: ?x sphn:property ?value .",
    "null": "There is no NULL in SPARQL. Leave the variable unbound: BIND(IF(BOUND(?p), ?g, ?unbound) AS ?x) or use OPTIONAL.",
}


def _masked(text: str) -> str:
    """Same length as text, with strings, IRIs and comments blanked, so brackets inside them are ignored."""
    return STRINGS_AND_IRIS.sub(lambda m: " " * len(m.group(0)), text)


def _enclosing_brace(masked: str, position: int) -> int | None:
    """Index of the innermost '{' open at position, or None at the top level."""
    depth = 0
    for i in range(position - 1, -1, -1):
        if masked[i] == "}":
            depth += 1
        elif masked[i] == "{":
            if depth == 0:
                return i
            depth -= 1
    return None


def _matching_close(masked: str, open_index: int) -> int | None:
    depth = 0
    for i in range(open_index, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _is_subselect(masked: str, open_index: int) -> bool:
    return bool(re.match(r"\{\s*SELECT\b", masked[open_index:], re.IGNORECASE))


def diagnose(text: str) -> list[str]:
    masked = _masked(text)
    hints: list[str] = []
    opened, closed = masked.count("("), masked.count(")")
    if opened != closed:
        hints.append(HINTS["parens"].format(opened=opened, closed=closed))
    if masked.count("{") != masked.count("}"):
        hints.append(f"Braces are not balanced: {masked.count('{')} '{{' but {masked.count('}')} '}}'. "
                     "Close every GRAPH, OPTIONAL, UNION branch and subquery block.")
    for m in re.finditer(r"\bGROUP\s+BY\b", masked, re.IGNORECASE):
        brace = _enclosing_brace(masked, m.start())
        if brace is not None and not _is_subselect(masked, brace):
            hints.append(HINTS["group_by_inside"])
            break
    m = re.search(r"\bGROUP\s+BY\s+(true|false|\d+)\b", masked, re.IGNORECASE)
    if m:
        hints.append(HINTS["group_by_constant"].format(value=m.group(1)))
    for m in re.finditer(r"\bUNION\b", masked, re.IGNORECASE):
        before = masked[:m.start()].rstrip()
        if not before.endswith("}"):
            continue
        close = len(before) - 1
        open_index = None
        depth = 0
        for i in range(close, -1, -1):
            if masked[i] == "}":
                depth += 1
            elif masked[i] == "{":
                depth -= 1
                if depth == 0:
                    open_index = i
                    break
        if open_index is None:
            continue
        head = masked[:open_index].rstrip()
        if re.search(r"\bGRAPH\s+(\?\w+|<[^>]*>|\S+:\S*)\s*$", head, re.IGNORECASE) or re.search(r"\bGRAPH\s*$", head, re.IGNORECASE):
            hints.append(HINTS["graph_union"])
        elif re.search(r"\bWHERE\s*$", head, re.IGNORECASE):
            hints.append(HINTS["union_outside"])
    where = re.search(r"\bWHERE\b", masked, re.IGNORECASE)
    select_clause = masked[: where.start()] if where else masked
    if re.search(r"\)\s*FILTER\s*\(", select_clause, re.IGNORECASE):
        hints.append(HINTS["aggregate_filter"])
    if re.search(r"\bOVER\s*\(", masked, re.IGNORECASE):
        hints.append(HINTS["window"])
    if re.search(r"\b(FILTER|IF)\s*\(\s*\?\w+\s+a\s+", masked, re.IGNORECASE):
        hints.append(HINTS["type_in_expression"])
    if re.search(r"\s(a|rdf:type)\s+\S+\s*\|", masked):
        hints.append(HINTS["class_alternatives"])
    for m in re.finditer(r"\{\s*SELECT\b", masked, re.IGNORECASE):
        close = _matching_close(masked, m.start())
        inner_where = re.search(r"\bWHERE\s*\{", masked[m.start():close or len(masked)], re.IGNORECASE)
        if close is None or inner_where is None:
            continue
        where_close = _matching_close(masked, m.start() + inner_where.end() - 1)
        if where_close is None:
            continue
        tail = masked[where_close + 1: close]
        # after the subselect's WHERE only solution modifiers may follow before its closing brace
        stripped = re.sub(r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET)\b[^{}]*", " ", tail, flags=re.IGNORECASE)
        if re.search(r"\b(BIND|FILTER|OPTIONAL|GRAPH|MINUS|VALUES)\b|\?\w+\s+\S+\s+\S+\s*\.", stripped, re.IGNORECASE):
            hints.append(HINTS["subselect_not_alone"])
            break
    if re.search(r"\?\w+\.\w+:", masked):
        hints.append(HINTS["member_access"])
    if re.search(r"\bNULL\b", masked):
        hints.append(HINTS["null"])
    return hints


def normalize(text: str) -> tuple[str, list[str]]:
    """Mechanical repairs whose meaning is unambiguous. Returns the text and what was changed."""
    applied: list[str] = []
    new = re.sub(r"\n[ \t]*GROUP\s+BY\s+(?:true|false|\d+)[ \t]*(?=\n|$)", "", text, flags=re.IGNORECASE)
    if new != text:
        applied.append("dropped GROUP BY <constant> (one overall group)")
        text = new
    # GRAPH ?v { ... } UNION  ->  { GRAPH ?v { ... } } UNION   (also when the GRAPH block follows UNION)
    changed = True
    while changed:
        changed = False
        masked = _masked(text)
        for m in re.finditer(r"\bGRAPH\s+(\?\w+|<[^>]*>)\s*\{", masked, re.IGNORECASE):
            close = _matching_close(masked, m.end() - 1)
            if close is None:
                continue
            after_union = re.match(r"\s*UNION\b", masked[close + 1:], re.IGNORECASE)
            before_union = re.search(r"\bUNION\s*$", masked[:m.start()], re.IGNORECASE)
            if after_union or before_union:
                text = text[:m.start()] + "{ " + text[m.start():close + 1] + " }" + text[close + 1:]
                applied.append("wrapped a GRAPH block that is a UNION branch in braces")
                changed = True
                break
    # GROUP BY ... written just before the closing brace of WHERE: move it after the brace
    masked = _masked(text)
    for m in re.finditer(r"\bGROUP\s+BY\b", masked, re.IGNORECASE):
        brace = _enclosing_brace(masked, m.start())
        if brace is None or _is_subselect(masked, brace):
            continue
        close = _matching_close(masked, brace)
        clause = text[m.start():close].rstrip()
        if close is not None and "{" not in masked[m.start():close] and _enclosing_brace(masked, brace) is None:
            text = text[:m.start()].rstrip() + "\n}\n" + clause + "\n" + text[close + 1:].lstrip("\n")
            applied.append("moved GROUP BY from inside WHERE to after its closing brace")
            break
    return text, applied
