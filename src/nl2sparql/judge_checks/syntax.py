"""Hints and mechanical repairs for a query that rdflib cannot parse.

rdflib's parse error names the token where its grammar gave up, often lines away from the mistake
("Expected SelectQuery, found 'GRAPH'" for a missing parenthesis in an IF chain), and the model
then repeats the error on its retry. The shapes recognized here are the ones the eval runs produced;
each hint names the mistake and says what to write instead. Six of the shapes have exactly one
meaning, so they are repaired instead of reported: a constant GROUP BY, a GROUP BY inside the WHERE
braces, a UNION branch that is a bare GRAPH block, the schema card's code placeholder copied as a
class (`?c a snomed:<code>`), a data property written without its sphn: prefix, and a scalar
subquery used as a value inside an expression.

The judge uses this module only after `parse_sparql` returned Invalid, so nothing here can change
the verdict of a valid query:

    fixed = add_missing_prefixes(fix_standard_prefixes(text))
    parsed = parse_sparql(fixed)
    if parsed.kind == "Invalid":
        repaired, applied = normalize(fixed, schema.data_terms)
        parsed = parse_sparql(repaired)
        if parsed.kind == "Invalid":
            errors = diagnose(repaired) or [parsed.error]

`normalize` returns the text byte for byte when no repair applies, and `diagnose` returns the hints
in priority order (the structural ones first), empty when no known shape matches.
"""
from __future__ import annotations

import re
from collections.abc import Collection

from nl2sparql.sparql.prefixes import add_missing_prefixes

HINTS = {
    "parentheses": "Parentheses are not balanced: {opened} '(' but {closed} ')'.",
    "parentheses_if_chain": " In BIND(IF(..., IF(..., ...))) close every IF and then the BIND. "
                            "Simpler: BIND(10 * FLOOR(?age / 10) AS ?decade).",
    "parentheses_unclosed": " The '(' that opens {snippet} is never closed: close every function call inside it "
                            "and then the (expression AS ?name).",
    "parentheses_extra": " The ')' ending {snippet} closes nothing: remove it or open the parenthesis it needs.",
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
    "placeholder": "{term} is the schema card's placeholder, not a code. To group by the kind of code write "
                   "?c a ?concept (no OPTION); to match one code use one from the code candidates.",
    "aggregate_filter": "SPARQL has no FILTER after an aggregate (COUNT(...) FILTER (...) is SQL). Count a subgroup "
                        "with OPTIONAL { GRAPH ?g { <condition> } BIND(?g AS ?x) } and COUNT(DISTINCT ?x), "
                        "or SUM(IF(<condition>, 1, 0)).",
    "window": "Window functions (OVER) do not exist in SPARQL. Compute the total in its own subquery "
              "{ SELECT (COUNT(DISTINCT ?g) AS ?total) WHERE { ... } } and divide by ?total.",
    "scalar_subquery": "A subquery cannot be a value inside an expression: (SELECT ...) in SELECT, BIND or FILTER "
                       "is invalid. Compute it once in WHERE, { SELECT (COUNT(DISTINCT ?g) AS ?total) WHERE "
                       "{ ... } }, use ?total in the expression and add ?total to GROUP BY.",
    "type_in_expression": "'a' (rdf:type) is a triple pattern, not an expression: FILTER(?x a <class>) and "
                          "IF(?x a <class>, ...) are invalid. Write ?x a <class> . as a pattern, or "
                          "?x a ?concept . VALUES (?concept ?label) { (<A> \"a\") (<B> \"b\") }.",
    "class_alternatives": "Class alternatives cannot be written as ?x a <A> | <B>. Write ?x a ?concept . "
                          "VALUES (?concept ?label) { (<A> \"a\") (<B> \"b\") } and GROUP BY ?label.",
    "subselect_not_alone": "A subquery { SELECT ... WHERE { ... } } must stand alone inside its braces. Move the "
                           "BIND/FILTER after its closing brace: { SELECT ... } BIND(...).",
    "member_access": "?x.sphn:property is not SPARQL. Bind the value with a triple pattern: ?x sphn:property ?value .",
    "null": "NULL does not exist in SPARQL: for a condition on a per-patient number use SUM(IF(?n >= 10, 1, 0)); "
            "for an absent pattern use OPTIONAL { GRAPH ?g { ... } BIND(?g AS ?x) } and COUNT(DISTINCT ?x).",
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
IF_BEFORE_RE = re.compile(r"(?<![\w?$:])IF\s*$", re.IGNORECASE)
IF_AFTER_RE = re.compile(r"\s*IF\s*\(", re.IGNORECASE)
AGGREGATE_FILTER_RE = re.compile(r"\)\s*FILTER\s*\(", re.IGNORECASE)
WINDOW_RE = re.compile(r"(?<![\w?$:])OVER\s*\(", re.IGNORECASE)
# A subquery written as a value: (SELECT COUNT(DISTINCT ?g2) WHERE { ... }) inside an expression.
SCALAR_SUBQUERY_RE = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
SCALAR_HEAD_RE = re.compile(r"\(\s*SELECT\s+(.*?)\s+WHERE\s*\{", re.IGNORECASE | re.DOTALL)
ALIASED_EXPRESSION_RE = re.compile(r"\(\s*(.*?)\s+AS\s+[?$](\w+)\s*\)", re.IGNORECASE | re.DOTALL)
AGGREGATE_CALL_RE = re.compile(r"(?<![\w?$:])(?:COUNT|SUM|AVG|MIN|MAX|SAMPLE|GROUP_CONCAT)\s*\(", re.IGNORECASE)
VARIABLE_RE = re.compile(r"[?$](\w+)")
GROUP_KEY_RE = re.compile(r"\s*[?$]\w+")
TYPE_IN_EXPRESSION_RE = re.compile(r"(?<![\w?$:])(?:FILTER|IF|BIND)\s*\(\s*\?\w+\s+(?:a|rdf:type)\s+", re.IGNORECASE)
# "?x a <A> | <B>": one bar, so that "||" (a logical or after a type pattern inside FILTER) is not an alternative.
CLASS_ALTERNATIVES_RE = re.compile(r"\s(?:a|rdf:type)\s+\S+\s*(?<!\|)\|(?!\|)")
MEMBER_ACCESS_RE = re.compile(r"\?\w+\.\w+:")
NULL_RE = re.compile(r"(?<![\w?$:])NULL\b", re.IGNORECASE)
BARE_IRI_RE = re.compile(r"(?<![\w<?$:/])(https?://[^\s<>\"{}|^`\\]+)")
BARE_PROPERTY_RE = re.compile(r"(?<![\w?$:<-])(has[A-Z]\w*)\b")
# The schema card's template "?c a snomed:<code>" copied literally (also icd-10-gm:<code>, chop:<code>).
PLACEHOLDER = r"[A-Za-z][\w-]*:<[A-Za-z_]+>"
PLACEHOLDER_RE = re.compile(PLACEHOLDER)
INFERENCE_OPTION = r"(?:\s*OPTION\s*\(\s*inference\s+(?:\"[^\"]*\"|'[^']*')\s*\))?"
PLACEHOLDER_TRIPLE_RE = re.compile(
    r"(\?\w+)\s+(?:a|rdf:type)\s+(" + PLACEHOLDER + r")" + INFERENCE_OPTION + r"[ \t]*(\.|;)?", re.IGNORECASE
)
PLACEHOLDER_CHAIN_RE = re.compile(r"\s*(;)\s*(?:a|rdf:type)\s+(" + PLACEHOLDER + r")" + INFERENCE_OPTION, re.IGNORECASE)
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
MAX_PASSES = 8  # one pass moves one GROUP BY; official-02's first try had two inside WHERE blocks
SNIPPET_CHARS = 60  # how much of the query's own text a hint quotes


def diagnose(text: str) -> list[str]:
    """Hints for a query rdflib rejected, most structural first; empty when no known shape matches."""
    masked = _masked(text)
    hints: list[str] = []
    opened, closed = masked.count("("), masked.count(")")
    if opened != closed:
        hints.append(HINTS["parentheses"].format(opened=opened, closed=closed) + _parenthesis_advice(text, masked))
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
    placeholder = _placeholder(text, masked)
    if placeholder:
        hints.append(HINTS["placeholder"].format(term=placeholder))
    if AGGREGATE_FILTER_RE.search(_select_clause(masked)):
        hints.append(HINTS["aggregate_filter"])
    if WINDOW_RE.search(masked):
        hints.append(HINTS["window"])
    if SCALAR_SUBQUERY_RE.search(masked):
        hints.append(HINTS["scalar_subquery"])
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


def normalize(text: str, data_terms: Collection[str] | None = None) -> tuple[str, list[str]]:
    """Repairs with exactly one meaning; returns the text (unchanged when nothing applies) and their names.

    `data_terms` are the class and property names the data has (SchemaCards.data_terms): a bare `hasX`
    among them gets its sphn: prefix. The passes repeat until nothing changes, because one pass moves
    one misplaced GROUP BY.
    """
    applied: list[str] = []
    for _ in range(MAX_PASSES):
        repaired, names = _repair_once(text, data_terms)
        if repaired == text:
            break
        applied.extend(names)
        text = repaired
    return text, applied


def _repair_once(text: str, data_terms: Collection[str] | None) -> tuple[str, list[str]]:
    applied: list[str] = []
    text, dropped = _drop_constant_group_by(text)
    applied.extend(dropped)
    text, dropped = _drop_card_placeholders(text)
    applied.extend(dropped)
    text, prefixed = _prefix_bare_properties(text, data_terms)
    applied.extend(prefixed)
    masked = _masked(text)
    if masked.count("{") == masked.count("}"):
        text, wrapped = _brace_graph_union_branches(text)
        applied.extend(wrapped)
        text, moved = _move_group_by_after_where(text)
        applied.extend(moved)
        text, hoisted = _hoist_scalar_subqueries(text)
        applied.extend(hoisted)
    return text, applied


def _masked(text: str) -> str:
    """Same length as text, with strings, IRIs and comments blanked, so positions stay valid."""
    return STRINGS_IRIS_COMMENTS_RE.sub(lambda match: " " * len(match.group(0)), text)


def _snippet(text: str, start: int = 0, end: int | None = None, tail: bool = False) -> str:
    """Up to SNIPPET_CHARS of text[start:end], quoted: its first line (last line when `tail`), or the
    whole piece on one line when that line is too short to recognize."""
    piece = text[start:end]
    line = piece.rsplit("\n", 1)[-1] if tail else piece.split("\n", 1)[0]
    piece = line.strip() if len(line.strip()) >= 12 else " ".join(piece.split())
    if len(piece) <= SNIPPET_CHARS:
        return f'"{piece}"'
    return f'"...{piece[-SNIPPET_CHARS:]}"' if tail else f'"{piece[:SNIPPET_CHARS]}..."'


def _parenthesis_advice(text: str, masked: str) -> str:
    """The IF-chain recipe when a parenthesis left open belongs to an IF chain; otherwise the query's own
    expression at fault, quoted, so the model does not rewrite an unrelated BIND (q-071)."""
    kind, indexes = _unbalanced_parenthesis(masked)
    if kind == "unclosed" and _inside_if_chain(masked, indexes):
        return HINTS["parentheses_if_chain"]
    if kind == "unclosed":
        return HINTS["parentheses_unclosed"].format(snippet=_snippet(text, _call_start(masked, indexes[0])))
    return HINTS["parentheses_extra"].format(snippet=_snippet(text, 0, indexes[0] + 1, tail=True))


def _unbalanced_parenthesis(masked: str) -> tuple[str, list[int]]:
    """("extra", [index of the first ')' that closes nothing]) or ("unclosed", indexes of the '(' left open,
    outermost first). Only called when the counts differ, so one of the two exists."""
    stack: list[int] = []
    for index, char in enumerate(masked):
        if char == "(":
            stack.append(index)
        elif char == ")":
            if not stack:
                return "extra", [index]
            stack.pop()
    return "unclosed", stack


def _inside_if_chain(masked: str, open_indexes: list[int]) -> bool:
    """A parenthesis left open is an IF call, or a call such as BIND whose argument starts with IF."""
    return any(IF_BEFORE_RE.search(masked[:index]) or IF_AFTER_RE.match(masked[index + 1:]) for index in open_indexes)


def _call_start(masked: str, open_index: int) -> int:
    """Where the call at an opening parenthesis starts (ROUND in ROUND(...)), or the parenthesis itself."""
    name = re.search(r"\w+$", masked[:open_index])
    return name.start() if name else open_index


def _placeholder(text: str, masked: str) -> str | None:
    """The first card placeholder (snomed:<code>) outside strings and comments, where the mask blanks it."""
    for match in PLACEHOLDER_RE.finditer(text):
        if masked[match.start()] == text[match.start()]:
            return match.group(0)
    return None


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


def _matching_close(masked: str, open_index: int, pair: str = "{}") -> int | None:
    depth = 0
    for index in range(open_index, len(masked)):
        if masked[index] == pair[0]:
            depth += 1
        elif masked[index] == pair[1]:
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


def _drop_card_placeholders(text: str) -> tuple[str, list[str]]:
    """?c a snomed:<code> [OPTION (...)] . -> nothing: the card's placeholder copied as a class. The variable
    keeps its other patterns, so the query means "any code of that property" (what intent-029 asked);
    inside a predicate chain only "; a snomed:<code>" goes, and a chain the placeholder opens keeps its subject."""
    applied = []
    for pattern in (PLACEHOLDER_CHAIN_RE, PLACEHOLDER_TRIPLE_RE):
        masked = _masked(text)  # the mask blanks <code> as an IRI, so the raw text is searched and the mask consulted
        for match in reversed(list(pattern.finditer(text))):
            if masked[match.start(1)] != text[match.start(1)]:
                continue  # inside a string or a comment
            if pattern is PLACEHOLDER_TRIPLE_RE:
                subject, term = match.group(1), match.group(2)
                if match.group(3) == ";":
                    text = text[:match.start()] + subject + " " + text[match.end():].lstrip(" \t")
                else:
                    text = _delete_span(text, match.start(), match.end())
                applied.append(f"dropped {subject} a {term}, the card's placeholder (any code)")
            else:
                text = text[:match.start()] + text[match.end():]
                applied.append(f"dropped a {match.group(2)}, the card's placeholder (any code)")
    return text, applied


def _prefix_bare_properties(text: str, data_terms: Collection[str] | None) -> tuple[str, list[str]]:
    """?x hasCode ?c -> ?x sphn:hasCode ?c when hasCode is a property of the data: a bare name is never valid
    SPARQL, and the data's has* properties all live in the sphn: namespace (q-032's dropped retry)."""
    if not data_terms:
        return text, []
    applied = []
    for match in reversed(list(BARE_PROPERTY_RE.finditer(_masked(text)))):
        term = match.group(1)
        if term in data_terms:
            text = text[:match.start(1)] + "sphn:" + text[match.start(1):]
            applied.append(f"wrote sphn:{term} for the bare {term}")
    return (add_missing_prefixes(text), applied) if applied else (text, applied)


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
    diagnose() to report. One call moves one clause; normalize() calls again until nothing changes.
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


def _hoist_scalar_subqueries(text: str) -> tuple[str, list[str]]:
    """(SELECT <aggregate> WHERE { ... }) used as a value  ->  ?scalar_1, bound by the same subquery hoisted to
    the top of WHERE as { SELECT (<aggregate> AS ?scalar_1) WHERE { ... } } and added to the top-level GROUP BY
    (q-027's first try, which Virtuoso would run but rdflib rejects). Only when every such subquery shares no
    variable with the rest of the query (a correlated one means something else per row), sits in the top-level
    SELECT clause or directly in the top-level WHERE, and holds nothing but one aggregate and its WHERE block;
    otherwise the text is left for diagnose() to explain. Identical subqueries share one variable.
    """
    original, masked = text, _masked(text)
    spans: list[tuple[int, int]] = []
    for match in SCALAR_SUBQUERY_RE.finditer(masked):
        close = _matching_close(masked, match.start(), "()")
        if close is None or any(start < match.start() < end for start, end in spans):
            return text, []
        spans.append((match.start(), close))
    if not spans or _group_by_inside_where(masked):
        return text, []
    outside_spans = [i for i, char in enumerate(masked) if char == "{" and not any(s < i < e for s, e in spans)]
    where_open = outside_spans[0] if outside_spans else None  # the top-level WHERE block
    if where_open is None or _matching_close(masked, where_open) is None:
        return text, []
    outside = "".join(char for i, char in enumerate(masked) if not any(s <= i <= e for s, e in spans))
    outer_variables = set(VARIABLE_RE.findall(outside))
    names: dict[str, str] = {}
    blocks: list[str] = []
    keys: list[str] = []
    for start, end in spans:
        if start > where_open and _enclosing_brace(masked, start) != where_open:
            return text, []  # inside a nested block or a sub-select, whose scope the hoisted block would not reach
        head = SCALAR_HEAD_RE.match(masked, start)
        block_close = _matching_close(masked, head.end() - 1) if head and head.end() <= end else None
        if block_close is None or block_close > end or masked[block_close + 1:end].strip():
            return text, []  # not "SELECT <expression> WHERE { ... }" alone
        if set(VARIABLE_RE.findall(masked[start:end + 1])) & outer_variables:
            return text, []  # correlated
        expression = text[head.start(1):head.end(1)].strip()
        aliased = ALIASED_EXPRESSION_RE.fullmatch(expression)
        expression, name = (aliased.group(1).strip(), aliased.group(2)) if aliased else (expression, None)
        if not AGGREGATE_CALL_RE.match(expression):
            return text, []  # not a single aggregate: not one value
        key = " ".join(text[start:end + 1].split())
        if key not in names:
            while name is None or name in outer_variables or name in names.values():
                name = f"scalar_{len(names) + 1}" if name is None else name + "_"
            names[key] = name
            blocks.append(f"{{ SELECT ({expression} AS ?{name}) WHERE {text[head.end() - 1:block_close + 1]} }}")
        keys.append(key)
    for (start, end), key in zip(reversed(spans), reversed(keys)):
        text = text[:start] + f"?{names[key]}" + text[end + 1:]
    masked = _masked(text)
    where_open = masked.index("{")
    text = text[:where_open + 1] + "".join(f"\n  {block}" for block in blocks) + text[where_open + 1:]
    masked = _masked(text)
    where_close = _matching_close(masked, where_open)
    extra = " ".join(f"?{name}" for name in names.values())
    group = GROUP_BY_RE.search(masked, where_close + 1)
    if group:
        insert_at = _end_of_group_keys(masked, group.end())
        if insert_at == group.end():
            return original, []  # a GROUP BY whose keys are not readable: not worth guessing
        text = text[:insert_at] + " " + extra + text[insert_at:]
    elif AGGREGATE_CALL_RE.search(masked[:where_open]):
        text = text[:where_close + 1] + f"\nGROUP BY {extra}" + text[where_close + 1:]
    applied = [f"hoisted the subquery {_snippet(key)} into WHERE as ?{name}" for key, name in names.items()]
    return text, applied


def _end_of_group_keys(masked: str, position: int) -> int:
    """Index after the last key of a GROUP BY clause that starts at position: variables and (expressions)."""
    end = position
    while True:
        key = GROUP_KEY_RE.match(masked, end)
        if key:
            end = key.end()
            continue
        parenthesis = re.match(r"\s*\(", masked[end:])
        close = _matching_close(masked, end + parenthesis.end() - 1, "()") if parenthesis else None
        if close is None:
            return end
        end = close + 1


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
