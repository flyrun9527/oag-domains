"""Ontology Builder for turning business documents into OAG domains."""

from .llm import DistillerLLM
from .pipeline import DistillerPipeline

__all__ = ["DistillerLLM", "DistillerPipeline"]
