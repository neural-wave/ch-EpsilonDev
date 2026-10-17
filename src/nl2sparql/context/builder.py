"""Builds the context the query agent sees. Pure code, no database access at request time."""
from __future__ import annotations

from nl2sparql.context.examples import ExampleBank
from nl2sparql.context.schema import SchemaCards
from nl2sparql.context.terminology import TerminologyIndex
from nl2sparql.models import Context


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
        # No candidate that only restates a class name; a candidate that is a value-set member pulls its class's card.
        code_candidates = self.terminology.candidates(text, self.k_codes, class_names=self.schema.classes)
        return Context(
            examples=examples,
            schema_cards=self.schema.cards_for(text, self.k_cards, examples_sparql=examples_sparql, codes=code_candidates),
            code_candidates=code_candidates,
        )
