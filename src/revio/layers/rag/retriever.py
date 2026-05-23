"""Retrieve relevant guideline chunks from the indexed vector store.

Thin wrapper around GuidelineIndexer's similarity search so agent tools
have a clean API to call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.documents import Document

from .indexer import GuidelineIndexer


logger = logging.getLogger(__name__)


class GuidelineRetriever:
    """Search the indexed guideline corpus by semantic similarity."""

    def __init__(self, repo_root: str | Path | None = None):
        self.indexer = GuidelineIndexer(repo_root=repo_root)

    def search(self, query: str, k: int = 5) -> list[Document]:
        """Top-k chunks by similarity to `query`."""
        if not query.strip():
            return []
        try:
            return self.indexer.vectorstore.similarity_search(query, k=k)
        except Exception as e:
            logger.debug("RAG search failed (vectorstore likely empty): %s", e)
            return []

    def search_with_scores(self, query: str, k: int = 5) -> list[tuple[Document, float]]:
        """Top-k chunks with relevance scores (higher = more similar)."""
        if not query.strip():
            return []
        try:
            return self.indexer.vectorstore.similarity_search_with_relevance_scores(query, k=k)
        except Exception as e:
            logger.debug("RAG search_with_scores failed: %s", e)
            return []

    def has_index(self) -> bool:
        """True if the index has any content."""
        return self.indexer.count() > 0
