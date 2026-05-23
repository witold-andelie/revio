"""Layer 1.5 — RAG over company / client coding guidelines.

The most common revio enterprise use case:
- A company has internal coding standards / security policies / framework
  conventions documented in markdown / pdf / docx.
- They drop those files into `.revio/guidelines/` (project) or
  `~/.config/revio/guidelines/` (user-global).
- The agent's `search_guidelines` tool queries them at review time and
  cites specific sections in its evidence chain.

This is *the* feature that separates revio from generic LLM review (Copilot
Review, Cursor Review, etc.) — those have zero awareness of company-internal
policy. revio does.
"""

from .document_loader import DocumentLoader
from .indexer import GuidelineIndexer
from .retriever import GuidelineRetriever

__all__ = ["DocumentLoader", "GuidelineIndexer", "GuidelineRetriever"]
