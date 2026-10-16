"""Standard prefixes of the SPHN data, and a fixer that declares the ones a query forgot."""
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
LEADING_DEFINES_RE = re.compile(r"(?:\s*DEFINE[^\n]*\n)*", re.IGNORECASE)
IRIS_AND_STRINGS_RE = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
PREFIX_USE_RE = re.compile(r"(?<![\w?$:-])([A-Za-z][\w-]*):")


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
