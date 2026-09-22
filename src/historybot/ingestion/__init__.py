"""Unified, evidence-preserving information ingestion."""

from .completeness import CompletenessChecker, TemplateRegistry
from .pipeline import IngestionService
from ..ai_ingestion import AIIngestionService
from .providers import (
    InputReader,
    OCRProvider,
    OfficeReader,
    PlainTextReader,
    RapidOCRProvider,
    TextOCRProvider,
    TrOCRProvider,
)

__all__ = [
    "CompletenessChecker",
    "AIIngestionService",
    "IngestionService",
    "InputReader",
    "OCRProvider",
    "OfficeReader",
    "PlainTextReader",
    "RapidOCRProvider",
    "TemplateRegistry",
    "TextOCRProvider",
    "TrOCRProvider",
]
