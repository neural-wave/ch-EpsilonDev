"""Hints and mechanical repairs for a query that rdflib cannot parse.

rdflib's parse error names the token where its grammar gave up, often lines away from the mistake
("Expected SelectQuery, found 'GRAPH'" for a missing parenthesis in an IF chain), and the model
then repeats the error on its retry. The shapes recognized here are the ones the eval runs produced;
each hint names the mistake and says what to write instead. Three of the shapes have exactly one
meaning, so they are repaired instead of reported.

The judge uses this module only after `parse_sparql` returned Invalid, so nothing here can change
the verdict of a valid query:

    fixed = add_missing_prefixes(fix_standard_prefixes(text))
    parsed = parse_sparql(fixed)
    if parsed.kind == "Invalid":
        repaired, applied = normalize(fixed)
        parsed = parse_sparql(repaired)
        if parsed.kind == "Invalid":
            errors = diagnose(repaired) or [parsed.error]

`normalize` returns the text byte for byte when no repair applies, and `diagnose` returns the hints
in priority order (the structural ones first), empty when no known shape matches.
"""
from __future__ import annotations

import re

HINTS = {
    "parentheses": "Parentheses are not balanced: {opened} '(' but {closed} ')'.",
    "parentheses_if_chain": " In BIND(IF(..., IF(..., ...))) close every IF and then the BIND. "
                            "Simpler: BIND(10 * FLOOR(?age / 10) AS ?decade).",
    "parentheses_plain": " Close every function call and every (expression AS ?name).",
    "braces": "Braces are not balanced: {opened} '{{' but {closed} '}}'. Close every WHERE, GRAPH, OPTIONAL, "
              "UNION branch and subquery block; GROUP BY, ORDER BY and LIMIT come after the closing brace of WHERE.",
    "group_by_inside": "GROUP BY is written inside the braces of WHERE. Close WHERE first: WHERE { ... } GROUP BY ?x "
                       "ORDER BY ... (GROUP BY, HAVING, ORDER BY and LIMIT come after the closing brace).",
    "group_by_constant": "GROUP BY {value} is not valid. For one overall row leave GROUP BY out; "
                         "otherwise group by a variable.",
    "graph_union": "Each UNION branch must be a braced group inside WHERE: "
                   "WHERE { { GRAPH ?g { ... } } UNION { GRAPH ?g { ... } } }.",
    "union_outside": "UNION appears outside the WHERE braces. Put both branches inside "
                     "WHERE { { ... } UNION { ... } }.",
    "aggregate_filter": "SPARQL has no FILTER after an aggregate (COUNT(...) FILTER (...) is SQL). Count a subgroup "
                        "with OPTIONAL { GRAPH ?g { <condition> } BIND(?g AS ?x) } and COUNT(DISTINCT ?x), "
                        "or SUM(IF(<condition>, 1, 0)).",
    "window": "Window functions (OVER) do not exist in SPARQL. Compute the total in its own subquery "
              "{ SELECT (COUNT(DISTINCT ?g) AS ?total) WHERE { ... } } and divide by ?total.",
    "type_in_expression": "'a' (rdf:type) is a triple pattern, not an expression: FILTER(?x a <class>) and "
                          "IF(?x a <class>, ...) are invalid. Write ?x a <class> . as a pattern, or "
                          "?x a ?concept . VALUES (?concept ?label) { (<A> \"a\") (<B> \"b\") }.",
    "class_alternatives": "Class alternatives cannot be written as ?x a <A> | <B>. Write ?x a ?concept . "
                          "VALUES (?concept ?label) { (<A> \"a\") (<B> \"b\") } and GROUP BY ?label.",
    "subselect_not_alone": "A subquery { SELECT ... WHERE { ... } } must stand alone inside its braces. Move the "
                           "BIND/FILTER after its closing brace: { SELECT ... } BIND(...).",
    "member_access": "?x.sphn:property is not SPARQL. Bind the value with a triple pattern: ?x sphn:property ?value .",
    "null": "There is no NULL in SPARQL. Leave the variable unbound: BIND(IF(BOUND(?p), ?g, ?unbound) AS ?x) "
            "or use OPTIONAL.",
    "bare_iri": "An IRI must be written in angle brackets: <{iri}>.",
    "bare_property": "{term} has no prefix. Write ?x sphn:{term} ?value .",
    "unknown_function": "{name}() is not a SPARQL function. Use a SPARQL 1.1 builtin (ROUND, FLOOR, YEAR, STR, "
                        "CONCAT, ...), a cast with its prefix (xsd:integer(?x)), bif:datediff(\"day\", ?a, ?b) "
                        "for dates, or an aggregate (COUNT, SUM, AVG, MIN, MAX).",
}

# Masked before any counting or scanning: brackets and keywords inside them are not syntax.
STRINGS_IRIS_COMMENTS_RE = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|#[^\n]*")
GROUP_BY_RE = re.compile(r"(?<![\w?$])GROUP\s+BY\b", re.IGNORECASE)
CONSTANT_GROUP_BY_RE = re.compile(r"(?<![\w?$])GROUP\s+BY\s+(true|false|\d+)\b(?!\s*[?(])", re.IGNORECASE)
UNION_RE = re.compile(r"(?<![\w?$])UNION\b", re.IGNORECASE)
GRAPH_BLOCK_RE = re.compile(r"(?<![\w?$])GRAPH\b[^{}]*\{", re.IGNORECASE)
GRAPH_HEAD_RE = re.compile(r"(?<![\w?$])GRAPH(\s+(\?\w+|[\w-]*:[\w./#-]*))?\s*$", re.IGNORECASE)
SUBSELECT_RE = re.compile(r"\{\s*SELECT\b", re.IGNORECASE)
# What may follow a WHERE block before the brace that closes a subquery: solution modifiers, then VALUES.
MODIFIERS_ONLY_RE = re.compile(
    r"(?:\s*(?:GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET)\b[^{}]*|\s*VALUES\b[^{}]*\{[^{}]*\})*\s*",
    re.IGNORECASE,
)
MODIFIER_KEYWORD_RE = re.compile(r"(?<![\w?$])(GROUP|HAVING|ORDER|LIMIT|OFFSET)\b", re.IGNORECASE)
MODIFIER_ORDER = {"GROUP": 0, "HAVING": 1, "ORDER": 2, "LIMIT": 3, "OFFSET": 4}
IF_CALL_RE = re.compile(r"(?<![\w?$:])IF\s*\(", re.IGNORECASE)
AGGREGATE_FILTER_RE = re.compile(r"\)\s*FILTER\s*\(", re.IGNORECASE)
WINDOW_RE = re.compile(r"(?<![\w?$:])OVER\s*\(", re.IGNORECASE)
TYPE_IN_EXPRESSION_RE = re.compile(r"(?<![\w?$:])(?:FILTER|IF|BIND)\s*\(\s*\?\w+\s+(?:a|rdf:type)\s+", re.IGNORECASE)
CLASS_ALTERNATIVES_RE = re.compile(r"\s(?:a|rdf:type)\s+\S+\s*\|")
MEMBER_ACCESS_RE = re.compile(r"\?\w+\.\w+:")
NULL_RE = re.compile(r"(?<![\w?$:])NULL\b", re.IGNORECASE)
BARE_IRI_RE = re.compile(r"(?<![\w<?$:/])(https?://[^\s<>\"{}|^`\\]+)")
BARE_PROPERTY_RE = re.compile(r"(?<![\w?$:<-])(has[A-Z]\w*)\b")
CALL_RE = re.compile(r"(?<![\w?$:.-])([A-Za-z_]\w*)\(")
# Every name that may precede "(" in SPARQL 1.1 plus Virtuoso's OPTION clause; anything else is not a function.
KNOWN_CALL_NAMES = frozenset(name.upper() for name in (
    "SELECT", "DISTINCT", "REDUCED", "FILTER", "BIND", "HAVING", "IF", "COALESCE", "IN", "NOT", "EXISTS",
    "ASC", "DESC", "VALUES", "OPTION", "BY",
    "STR", "LANG", "LANGMATCHES", "DATATYPE", "BOUND", "IRI", "URI", "BNODE", "RAND", "ABS", "CEIL", "FLOOR",
    "ROUND", "CONCAT", "STRLEN", "UCASE", "LCASE", "ENCODE_FOR_URI", "CONTAINS", "STRSTARTS", "STRENDS",
    "STRBEFORE", "STRAFTER", "YEAR", "MONTH", "DAY", "HOURS", "MINUTES", "SECONDS", "TIMEZONE", "TZ", "NOW",
    "UUID", "STRUUID", "MD5", "SHA1", "SHA256", "SHA384", "SHA512", "STRLANG", "STRDT", "SAMETERM", "ISIRI",
    "ISURI", "ISBLANK", "ISLITERAL", "ISNUMERIC", "REGEX", "SUBSTR", "REPLACE",
    "COUNT", "SUM", "MIN", "MAX", "AVG", "SAMPLE", "GROUP_CONCAT",
))


def diagnose(text: str) -> list[str]:
    """Hints for a query rdflib rejected, most structural first; empty when no known shape matches."""
    masked = _masked(text)
    hints: list[str] = []
    opened, closed = masked.count("("), masked.count(")")
    if opened != closed:
        advice = HINTS["parentheses_if_chain"] if IF_CALL_RE.search(masked) else HINTS["parentheses_plain"]
        hints.append(HINTS["parentheses"].format(opened=opened, closed=closed) + advice)
    opened, closed = masked.count("{"), masked.count("}")
    balanced = opened == closed
    if not balanced:
        hints.append(HINTS["braces"].format(opened=opened, closed=closed))
    # The brace-structure checks assume matching braces; with unbalanced ones the counts hint is the real error.
    if balanced and _group_by_inside_where(masked):
        hints.append(HINTS["group_by_inside"])
    constant = CONSTANT_GROUP_BY_RE.search(masked)
    if constant:
        hints.append(HINTS["group_by_constant"].format(value=constant.group(1)))
    if balanced:
        hints.extend(_union_hints(masked))
    if AGGREGATE_FILTER_RE.search(_select_clause(masked)):
        hints.append(HINTS["aggregate_filter"])
    if WINDOW_RE.search(masked):
        hints.append(HINTS["window"])
    if TYPE_IN_EXPRESSION_RE.search(masked):
        hints.append(HINTS["type_in_expression"])
    if CLASS_ALTERNATIVES_RE.search(masked):
        hints.append(HINTS["class_alternatives"])
    if balanced and _subselect_followed_by_patterns(masked):
        hints.append(HINTS["subselect_not_alone"])
    if MEMBER_ACCESS_RE.search(masked):
        hints.append(HINTS["member_access"])
    if NULL_RE.search(masked):
        hints.append(HINTS["null"])
    bare_iri = BARE_IRI_RE.search(masked)
    if bare_iri:
        hints.append(HINTS["bare_iri"].format(iri=bare_iri.group(1)))
    bare_property = BARE_PROPERTY_RE.search(masked)
    if bare_property:
        hints.append(HINTS["bare_property"].format(term=bare_property.group(1)))
    for call in CALL_RE.finditer(masked):
        if call.group(1).upper() not in KNOWN_CALL_NAMES:
            hints.append(HINTS["unknown_function"].format(name=call.group(1)))
            break
    return list(dict.fromkeys(hints))


def normalize(text: str) -> tuple[str, list[str]]:
    """Repairs with exactly one meaning; returns the text (unchanged when nothing applies) and their names."""
    applied: list[str] = []
    text, dropped = _drop_constant_group_by(text)
    applied.extend(dropped)
    masked = _masked(text)
    if masked.count("{") == masked.count("}"):
        text, wrapped = _brace_graph_union_branches(text)
        applied.extend(wrapped)
        text, moved = _move_group_by_after_where(text)
        applied.extend(moved)
    return text, applied


def _masked(text: str) -> str:
    """Same length as text, with strings, IRIs and comments blanked, so positions stay valid."""
    return STRINGS_IRIS_COMMENTS_RE.sub(lambda match: " " * len(match.group(0)), text)


def _enclosing_brace(masked: str, position: int) -> int | None:
    """Index of the innermost '{' still open at position, or None at the top level."""
    depth = 0
    for index in range(position - 1, -1, -1):
        if masked[index] == "}":
            depth += 1
        elif masked[index] == "{":
            if depth == 0:
                return index
            depth -= 1
    return None


def _matching_close(masked: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _matching_open(masked: str, close_index: int) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        if masked[index] == "}":
            depth += 1
        elif masked[index] == "{":
            depth -= 1
            if depth == 0:
                return index
    return None


def _is_subselect(masked: str, open_index: int) -> bool:
    return SUBSELECT_RE.match(masked, open_index) is not None


def _is_where_block(masked: str, open_index: int) -> bool:
    """The block of a SELECT's WHERE: at the top level or right after the WHERE keyword, never a subquery."""
    if _is_subselect(masked, open_index):
        return False
    if _enclosing_brace(masked, open_index) is None:
        return True
    return re.search(r"\bWHERE\s*$", masked[:open_index], re.IGNORECASE) is not None


def _select_clause(masked: str) -> str:
    first_brace = masked.find("{")
    return masked if first_brace == -1 else masked[:first_brace]


def _group_by_inside_where(masked: str) -> bool:
    for match in GROUP_BY_RE.finditer(masked):
        enclosing = _enclosing_brace(masked, match.start())
        if enclosing is not None and not _is_subselect(masked, enclosing):
            return True
    return False


def _union_hints(masked: str) -> list[str]:
    hints = []
    for match in UNION_RE.finditer(masked):
        if re.match(r"\s*GRAPH\b", masked[match.end():], re.IGNORECASE):
            hints.append(HINTS["graph_union"])
        before = masked[:match.start()].rstrip()
        if not before.endswith("}"):
            continue
        open_index = _matching_open(masked, len(before) - 1)
        if open_index is None:
            continue
        if GRAPH_HEAD_RE.search(masked[:open_index]):
            hints.append(HINTS["graph_union"])
        elif _enclosing_brace(masked, open_index) is None:
            hints.append(HINTS["union_outside"])
    return hints


def _subselect_followed_by_patterns(masked: str) -> bool:
    """A subquery whose braces hold more than SELECT, its WHERE block and solution modifiers."""
    for match in SUBSELECT_RE.finditer(masked):
        close = _matching_close(masked, match.start())
        where_open = masked.find("{", match.end())
        if close is None or where_open == -1 or where_open > close:
            continue
        where_close = _matching_close(masked, where_open)
        if where_close is None or where_close > close:
            continue
        if not MODIFIERS_ONLY_RE.fullmatch(masked[where_close + 1:close]):
            return True
    return False


def _drop_constant_group_by(text: str) -> tuple[str, list[str]]:
    applied = []
    for match in reversed(list(CONSTANT_GROUP_BY_RE.finditer(_masked(text)))):
        text = _delete_span(text, match.start(), match.end())
        applied.append(f"dropped GROUP BY {match.group(1)} (one overall group)")
    return text, applied


def _delete_span(text: str, start: int, end: int) -> str:
    """Remove text[start:end], and its whole line when nothing else is on it."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    if not text[line_start:start].strip() and not text[end:line_end].strip():
        return text[:line_start] + text[min(line_end + 1, len(text)):]
    return text[:start] + text[end:]


def _brace_graph_union_branches(text: str) -> tuple[str, list[str]]:
    """GRAPH ?g { ... } UNION { ... }  ->  { GRAPH ?g { ... } } UNION { ... }, on either side of the UNION."""
    applied = []
    wrapped = True
    while wrapped:
        wrapped = False
        masked = _masked(text)
        for match in GRAPH_BLOCK_RE.finditer(masked):
            close = _matching_close(masked, match.end() - 1)
            if close is None:
                continue
            union_after = re.match(r"\s*UNION\b", masked[close + 1:], re.IGNORECASE)
            union_before = re.search(r"\bUNION\s*$", masked[:match.start()], re.IGNORECASE)
            if union_after or union_before:
                text = text[:match.start()] + "{ " + text[match.start():close + 1] + " }" + text[close + 1:]
                applied.append("wrapped a GRAPH block that is a UNION branch in braces")
                wrapped = True
                break
    return text, applied


def _move_group_by_after_where(text: str) -> tuple[str, list[str]]:
    """WHERE { ... GROUP BY ?x }  ->  WHERE { ... } GROUP BY ?x, when the clause is only solution modifiers.

    When the same clause already follows the brace, the inner copy is dropped instead; when a different
    modifier clause follows and the two cannot be concatenated in grammar order, the text is left for
    diagnose() to report.
    """
    masked = _masked(text)
    for match in GROUP_BY_RE.finditer(masked):
        open_index = _enclosing_brace(masked, match.start())
        if open_index is None or not _is_where_block(masked, open_index):
            continue
        close = _matching_close(masked, open_index)
        if close is None or not MODIFIERS_ONLY_RE.fullmatch(masked[match.start():close]):
            continue
        clause = text[match.start():close].strip()
        outer = masked[close + 1:_end_of_block(masked, close + 1)]
        if " ".join(outer.split()).startswith(" ".join(clause.split())):
            text = text[:match.start()].rstrip() + "\n" + text[close:]
            return text, ["dropped a GROUP BY inside WHERE that repeats the one after it"]
        inner_order = [MODIFIER_ORDER[keyword.upper()] for keyword in MODIFIER_KEYWORD_RE.findall(clause)]
        outer_order = [MODIFIER_ORDER[keyword.upper()] for keyword in MODIFIER_KEYWORD_RE.findall(outer)]
        if outer_order and max(inner_order) >= min(outer_order):
            continue
        text = text[:match.start()].rstrip() + "\n}\n" + clause + "\n" + text[close + 1:].lstrip("\n")
        return text, ["moved GROUP BY from inside WHERE to after its closing brace"]
    return text, []


def _end_of_block(masked: str, position: int) -> int:
    """Index of the '}' that closes the block containing position, or the end of the text."""
    depth = 0
    for index in range(position, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            if depth == 0:
                return index
            depth -= 1
    return len(masked)
