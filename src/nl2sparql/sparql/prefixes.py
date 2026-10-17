"""Standard prefixes of the SPHN data, and fixers for declarations a query forgot or got wrong."""
from __future__ import annotations

import re

SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
INFERENCE_RULE_SET = "https://biomedit.ch/rdf/sphn-resource/global"

STANDARD_PREFIXES = {
    "sphn": SPHN,
    "sphn-individual": "https://biomedit.ch/rdf/sphn-schema/sphn/individual#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "icd-10-gm": "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "chop": "https://biomedit.ch/rdf/sphn-resource/chop/",
    "snomed": "http://snomed.info/id/",
    "loinc": "https://loinc.org/rdf/",
    "ucum": "https://biomedit.ch/rdf/sphn-resource/ucum/",
    "atc": "https://www.whocc.no/atc_ddd_index/?code=",
    "geno": "http://purl.obolibrary.org/obo/GENO_",
    "ordo": "http://www.orpha.net/ORDO/Orphanet_",
    "bif": "bif:",
}

PREFIX_DECLARATION_RE = re.compile(r"^\s*PREFIX\s+([A-Za-z][\w.-]*)?\s*:\s*<", re.IGNORECASE | re.MULTILINE)
PREFIX_NAMESPACE_RE = re.compile(r"((?<![\w?$:])PREFIX\s+([A-Za-z][\w.-]*)\s*:\s*)<([^<>\s]*)>", re.IGNORECASE)
LEADING_DEFINES_RE = re.compile(r"(?:\s*DEFINE[^\n]*\n)*", re.IGNORECASE)
IRIS_AND_STRINGS_RE = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
STRINGS_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
PREFIX_USE_RE = re.compile(r"(?<![\w?$:-])([A-Za-z][\w-]*):")


def fix_standard_prefixes(text: str) -> str:
    """Point a standard prefix name declared with another namespace back at the data's namespace.

    The model declares `icd-10-gm:` as the OBO namespace, `geno:` under biomedit or `snomed:` as
    `http://snomed.info/sid/`, and every term under it then names nothing in the data. `bif:` stays
    as declared (`<bif:>` is the accepted form), and a standard namespace declared under another
    prefix name is fine. The text comes back unchanged when every declaration is right.
    """
    without_strings = STRINGS_RE.sub(lambda match: " " * len(match.group(0)), text)
    for match in reversed(list(PREFIX_NAMESPACE_RE.finditer(without_strings))):
        head, name, namespace = match.groups()
        standard = STANDARD_PREFIXES.get(name)
        if standard is None or name == "bif" or namespace == standard:
            continue
        text = text[:match.start()] + f"{head}<{standard}>" + text[match.end():]
    return text


def add_missing_prefixes(text: str) -> str:
    """Declare standard prefixes that the query uses without declaring them."""
    declared = {match.group(1) or "" for match in PREFIX_DECLARATION_RE.finditer(text)}
    body = IRIS_AND_STRINGS_RE.sub(" ", text)
    used = set(PREFIX_USE_RE.findall(body))
    missing = [name for name in STANDARD_PREFIXES if name in used and name not in declared]
    if not missing:
        return text
    header = "".join(f"PREFIX {name}: <{STANDARD_PREFIXES[name]}>\n" for name in missing)
    defines_end = LEADING_DEFINES_RE.match(text).end()
    return text[:defines_end] + header + text[defines_end:]
