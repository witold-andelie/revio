"""ChromaDB-backed guideline indexer.

One Chroma collection per repository. Persisted at:
    ~/.cache/revio/<repo-hash>/vectorstore/

Singleton pattern at the class level keeps the embedding model + collection
loaded across multiple `GuidelineIndexer()` instances in the same session
(otherwise sentence-transformers reloads ~80MB every time).

Adapted from v1's src/rag/indexer.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from langchain_core.documents import Document


logger = logging.getLogger(__name__)


# Sentence-Transformers model (~80MB, downloads on first use to HF cache)
DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"


class GuidelineIndexer:
    """Indexes guideline documents into a per-repo ChromaDB collection."""

    # Class-level singletons (avoid reload across instances in same process)
    _shared_embeddings = None
    _shared_vectorstores: dict[str, "object"] = {}

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        persist_dir: str | Path | None = None,
        embedding_model: str = DEFAULT_EMBED_MODEL,
    ):
        if persist_dir is None:
            # Default: per-repo persist location
            if repo_root is None:
                base = Path.home() / ".cache" / "revio" / "default"
            else:
                repo_root_resolved = Path(repo_root).expanduser().resolve()
                repo_hash = hashlib.sha1(str(repo_root_resolved).encode()).hexdigest()[:12]
                base = Path.home() / ".cache" / "revio" / repo_hash
            persist_dir = base / "vectorstore"

        self.persist_dir = Path(persist_dir).expanduser().resolve()
        self.embedding_model = embedding_model

    # ---- Lazy singletons ----

    @property
    def embeddings(self):
        if GuidelineIndexer._shared_embeddings is None:
            try:
                from langchain_huggingface import HuggingFaceEmbeddings
            except ImportError:
                # Fallback for environments without the newer split package
                from langchain_community.embeddings import HuggingFaceEmbeddings

            logger.info("loading embedding model %s ...", self.embedding_model)
            GuidelineIndexer._shared_embeddings = HuggingFaceEmbeddings(
                model_name=self.embedding_model
            )
        return GuidelineIndexer._shared_embeddings

    @property
    def vectorstore(self):
        key = str(self.persist_dir)
        if key not in GuidelineIndexer._shared_vectorstores:
            from langchain_chroma import Chroma

            os.makedirs(self.persist_dir, exist_ok=True)
            GuidelineIndexer._shared_vectorstores[key] = Chroma(
                persist_directory=str(self.persist_dir),
                embedding_function=self.embeddings,
                collection_name="revio_guidelines",
            )
        return GuidelineIndexer._shared_vectorstores[key]

    # ---- Public API ----

    def index_documents(self, documents: list[Document]) -> int:
        """Add a list of Documents to the index. Returns the count added."""
        if not documents:
            return 0
        try:
            self.vectorstore.add_documents(documents)
        except Exception as e:
            logger.error("index_documents failed: %s", e)
            return 0
        return len(documents)

    def index_file(self, file_path: str | Path) -> int:
        """Load + index a single file."""
        from .document_loader import DocumentLoader

        docs = DocumentLoader.load_file(file_path)
        return self.index_documents(docs)

    def index_directory(self, dir_path: str | Path) -> int:
        """Load + index all guideline files in a directory."""
        from .document_loader import DocumentLoader

        docs = DocumentLoader.load_directory(dir_path)
        return self.index_documents(docs)

    def delete_by_source(self, source_path: str | Path) -> int:
        """Remove all chunks originating from a specific source file."""
        try:
            source = str(Path(source_path).expanduser().resolve())
            self.vectorstore.delete(where={"source": source})
        except Exception as e:
            logger.warning("delete_by_source failed: %s", e)
            return 0
        return 1

    def clear(self) -> None:
        """Drop the entire collection."""
        try:
            self.vectorstore.delete_collection()
        except Exception:
            pass
        # Force recreation on next access
        key = str(self.persist_dir)
        GuidelineIndexer._shared_vectorstores.pop(key, None)

    def count(self) -> int:
        """Return the number of chunks in the index."""
        try:
            # Chroma's internal collection exposes count()
            collection = self.vectorstore._collection
            return collection.count()
        except Exception:
            return 0

    def list_sources(self) -> list[str]:
        """Return distinct source file paths currently indexed."""
        try:
            data = self.vectorstore._collection.get(include=["metadatas"])
            metas = data.get("metadatas") or []
            return sorted({m.get("source") for m in metas if m and m.get("source")})
        except Exception:
            return []
