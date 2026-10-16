"""Parse Virtuoso-flavoured SPARQL into rdflib algebra.

Virtuoso accepts two extensions that standard parsers reject: `DEFINE ...` pragma lines and
`OPTION (inference "...")` clauses after a triple pattern. We remove both before parsing and
remember that they were there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from rdflib.plugins.sparql.sparql import Query

DEFINE_RE = re.compile(r"^\s*DEFINE\s+\S+\s+(\"[^\"]*\"|'[^']*'|\S+)\s*$", re.IGNORECASE | re.MULTILINE)
INFERENCE_RE = re.compile(r"OPTION\s*\(\s*inference\s+(\"[^\"]*\"|'[^']*')\s*\)", re.IGNORECASE)


@dataclass
class ParsedQuery:
    text: str
    kind: str  # SelectQuery | ConstructQuery | AskQuery | DescribeQuery | Update | Invalid
    query: Query | None = None
    error: str | None = None
    defines: list[str] = field(default_factory=list)
    inference_options: int = 0

    @property
    def algebra(self):
        return self.query.algebra if self.query is not None else None


def strip_virtuoso_extensions(text: str) -> tuple[str, list[str], int]:
    """Return the text without DEFINE lines and OPTION (inference ...) clauses."""
    defines = [match.group(0).strip() for match in DEFINE_RE.finditer(text)]
    without_defines = DEFINE_RE.sub("", text)
    stripped, inference_count = INFERENCE_RE.subn(" ", without_defines)
    return stripped, defines, inference_count


def parse_sparql(text: str) -> ParsedQuery:
    stripped, defines, inference_count = strip_virtuoso_extensions(text)
    try:
        query = translateQuery(parseQuery(stripped))
    except Exception as query_error:  # rdflib raises pyparsing and plain exceptions
        try:
            parseUpdate(stripped)
        except Exception:
            message = str(query_error).splitlines()[0][:300] if str(query_error) else type(query_error).__name__
            return ParsedQuery(text, "Invalid", error=message, defines=defines, inference_options=inference_count)
        return ParsedQuery(text, "Update", defines=defines, inference_options=inference_count)
    return ParsedQuery(text, query.algebra.name, query=query, defines=defines, inference_options=inference_count)
