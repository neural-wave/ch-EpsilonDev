"""Builds the context the query agent sees. Pure code, no database access at request time."""
from __future__ import annotations

from dataclasses import replace

from nl2sparql.context.examples import ExampleBank
from nl2sparql.context.schema import SchemaCards
from nl2sparql.context.terminology import TerminologyIndex
from nl2sparql.models import CodeEntry, Context

SHOWN_LABELS = 2  # the query prompt prints the first two labels of a code candidate (agents/prompts.py)


class ContextBuilder:
    def __init__(
        self,
        examples: ExampleBank,
        schema: SchemaCards,
        terminology: TerminologyIndex,
        k_examples: int = 3,
        k_cards: int = 6,
        k_codes: int = 5,
        exclude_exact_examples: bool = False,
    ):
        self.examples = examples
        self.schema = schema
        self.terminology = terminology
        self.k_examples = k_examples
        self.k_cards = k_cards
        self.k_codes = k_codes
        self.exclude_exact_examples = exclude_exact_examples

    def build(self, text: str) -> Context:
        examples = self.examples.search(text, self.k_examples, exclude_exact=self.exclude_exact_examples)
        # Classes named in the question first, then the ones the retrieved examples use.
        examples_sparql = "\n".join(example.sparql for example in examples)
        # No candidate that only restates a class name; a candidate that is a value-set member pulls its class's
        # card, one whose kind a class holds (a geographic location: Country) that class and the records linking to it.
        code_candidates = self.terminology.candidates(text, self.k_codes, class_names=self.schema.classes)
        return Context(
            examples=examples,
            schema_cards=self.schema.cards_for(text, self.k_cards, examples_sparql=examples_sparql, codes=code_candidates),
            code_candidates=[self._with_use(entry) for entry in code_candidates],
        )

    def _with_use(self, entry: CodeEntry) -> CodeEntry:
        """The candidate with where its kind is used after the labels the prompt shows: "<country> (geographic
        location); used as: sphn:Country hasCode (via ...)". Without the line, "How many patients live in
        India?" typed the country code on OncologyDiagnosis and RadiotherapyProcedure."""
        home = self.schema.code_home(entry)
        if home is None:
            return entry
        shown = list(entry.labels[:SHOWN_LABELS])
        use = f"used as: {home.describe()}"
        shown = [*shown[:-1], f"{shown[-1]}; {use}"] if shown else [use]
        return replace(entry, labels=(*shown, *entry.labels[SHOWN_LABELS:]))
