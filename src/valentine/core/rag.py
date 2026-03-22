# src/valentine/agents/rag.py
"""
Codebase RAG (Retrieval Augmented Generation) for Valentine.

Indexes local project files into Qdrant for semantic search.
Allows Valentine to instantly answer "Where is the auth logic?"
without reading every file each time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from valentine.config import settings

logger = logging.getLogger(__name__)

# File extensions to index
INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".rb", ".php", ".c", ".cpp", ".h", ".css", ".scss",
    ".html", ".md", ".txt", ".yaml", ".yml", ".toml", ".json",
    ".sh", ".bash", ".sql", ".graphql", ".proto",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".cache", "coverage",
    ".tox", ".mypy_cache", ".ruff_cache", "egg-info",
}

MAX_FILE_SIZE = 100_000  # 100KB max per file
CHUNK_SIZE = 500  # characters per chunk (with overlap)
CHUNK_OVERLAP = 100


@dataclass
class CodeChunk:
    """A chunk of code with metadata."""
    content: str
    file_path: str
    start_line: int
    end_line: int
    language: str
    file_hash: str


@dataclass
class SearchResult:
    """A search result from the RAG system."""
    content: str
    file_path: str
    start_line: int
    end_line: int
    score: float
    language: str


class CodebaseRAG:
    """
    Indexes project files into Qdrant for semantic code search.

    Uses sentence-transformers for embeddings (already a Valentine dependency)
    and Qdrant (already running for Mem0).
    """

    COLLECTION_NAME = "valentine_codebase"

    def __init__(self):
        self._qdrant = None
        self._embedder = None
        self._initialized = False

    async def _init(self):
        """Lazy initialization of Qdrant client and embedder."""
        if self._initialized:
            return True

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams, PointStruct
            from sentence_transformers import SentenceTransformer

            self._qdrant = QdrantClient(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
            )

            # Use the same model as Mem0 for consistency
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            self._embedding_dim = self._embedder.get_sentence_embedding_dimension()

            # Create collection if it doesn't exist
            collections = [c.name for c in self._qdrant.get_collections().collections]
            if self.COLLECTION_NAME not in collections:
                self._qdrant.create_collection(
                    collection_name=self.COLLECTION_NAME,
                    vectors_config=VectorParams(
                        size=self._embedding_dim,
                        distance=Distance.COSINE,
                    ),
                )

            self._initialized = True
            return True
        except ImportError as e:
            logger.warning(f"RAG dependencies not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize RAG: {e}")
            return False

    def _chunk_file(self, file_path: str) -> list[CodeChunk]:
        """Split a file into overlapping chunks."""
        try:
            with open(file_path, "r", errors="replace") as f:
                content = f.read()
        except Exception:
            return []

        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE]

        ext = Path(file_path).suffix
        language = ext.lstrip(".")
        file_hash = hashlib.md5(content.encode()).hexdigest()[:12]

        lines = content.split("\n")
        chunks = []

        # Chunk by lines, roughly CHUNK_SIZE characters each
        current_chunk = []
        current_size = 0
        start_line = 1

        for i, line in enumerate(lines, 1):
            current_chunk.append(line)
            current_size += len(line) + 1

            if current_size >= CHUNK_SIZE:
                chunk_text = "\n".join(current_chunk)
                chunks.append(CodeChunk(
                    content=chunk_text,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=i,
                    language=language,
                    file_hash=file_hash,
                ))

                # Overlap: keep last few lines
                overlap_lines = max(1, len(current_chunk) // 5)
                current_chunk = current_chunk[-overlap_lines:]
                current_size = sum(len(l) + 1 for l in current_chunk)
                start_line = i - overlap_lines + 1

        # Last chunk
        if current_chunk:
            chunks.append(CodeChunk(
                content="\n".join(current_chunk),
                file_path=file_path,
                start_line=start_line,
                end_line=len(lines),
                language=language,
                file_hash=file_hash,
            ))

        return chunks

    def _scan_directory(self, directory: str) -> list[str]:
        """Recursively find indexable files."""
        files = []
        for root, dirs, filenames in os.walk(directory):
            # Skip unwanted directories
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

            for fname in filenames:
                ext = Path(fname).suffix
                if ext in INDEXABLE_EXTENSIONS:
                    full_path = os.path.join(root, fname)
                    if os.path.getsize(full_path) <= MAX_FILE_SIZE:
                        files.append(full_path)

        return sorted(files)

    async def index_directory(self, directory: str) -> int:
        """
        Index all code files in a directory.

        Returns the number of chunks indexed.
        """
        if not await self._init():
            return 0

        from qdrant_client.models import PointStruct

        files = self._scan_directory(directory)
        if not files:
            logger.warning(f"No indexable files found in {directory}")
            return 0

        logger.info(f"Indexing {len(files)} files from {directory}")

        all_chunks = []
        for file_path in files:
            chunks = self._chunk_file(file_path)
            all_chunks.extend(chunks)

        if not all_chunks:
            return 0

        # Batch embed all chunks
        texts = [f"{c.file_path}:{c.start_line}\n{c.content}" for c in all_chunks]

        # Run embedding in executor (CPU-bound)
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, self._embedder.encode, texts)

        # Upsert into Qdrant in batches
        batch_size = 100
        total = 0
        for i in range(0, len(all_chunks), batch_size):
            batch_chunks = all_chunks[i:i + batch_size]
            batch_embeddings = embeddings[i:i + batch_size]

            points = []
            for j, (chunk, embedding) in enumerate(zip(batch_chunks, batch_embeddings)):
                point_id = abs(hash(f"{chunk.file_path}:{chunk.start_line}:{chunk.file_hash}")) % (2**63)
                points.append(PointStruct(
                    id=point_id,
                    vector=embedding.tolist(),
                    payload={
                        "content": chunk.content,
                        "file_path": chunk.file_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "language": chunk.language,
                        "file_hash": chunk.file_hash,
                    },
                ))

            self._qdrant.upsert(
                collection_name=self.COLLECTION_NAME,
                points=points,
            )
            total += len(points)

        logger.info(f"Indexed {total} chunks from {len(files)} files")
        return total

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Search the indexed codebase semantically."""
        if not await self._init():
            return []

        # Embed the query
        loop = asyncio.get_event_loop()
        query_embedding = await loop.run_in_executor(
            None, self._embedder.encode, query
        )

        results = self._qdrant.search(
            collection_name=self.COLLECTION_NAME,
            query_vector=query_embedding.tolist(),
            limit=limit,
        )

        return [
            SearchResult(
                content=r.payload["content"],
                file_path=r.payload["file_path"],
                start_line=r.payload["start_line"],
                end_line=r.payload["end_line"],
                score=r.score,
                language=r.payload.get("language", ""),
            )
            for r in results
        ]

    async def search_formatted(self, query: str, limit: int = 5) -> str:
        """Search and return formatted results for LLM context."""
        results = await self.search(query, limit)
        if not results:
            return "No relevant code found. The codebase may not be indexed yet."

        formatted = []
        for r in results:
            formatted.append(
                f"--- {r.file_path}:{r.start_line}-{r.end_line} "
                f"(score: {r.score:.2f}) ---\n{r.content}"
            )
        return "\n\n".join(formatted)

    async def get_stats(self) -> dict:
        """Get collection statistics."""
        if not await self._init():
            return {"error": "RAG not initialized"}

        info = self._qdrant.get_collection(self.COLLECTION_NAME)
        return {
            "total_chunks": info.points_count,
            "vectors_count": info.vectors_count,
            "status": info.status.value,
        }

    async def clear(self):
        """Clear the entire index."""
        if not await self._init():
            return
        self._qdrant.delete_collection(self.COLLECTION_NAME)
        self._initialized = False  # will recreate on next init
