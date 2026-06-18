"""
embedder.py — Embedding generation and FAISS index management.

Responsibilities:
  1. Load the sentence-transformer model (once, as a singleton)
  2. Embed chunks in batches (memory efficient)
  3. Build and persist a FAISS index per repository
  4. Load an existing index and run similarity search

Key design choices explained inline.
"""

import json
import logging
import numpy as np
import faiss
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass
from sentence_transformers import SentenceTransformer

from backend.config import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# all-MiniLM-L6-v2:
#   - 384 dimensions (small → fast)
#   - Trained on 1B+ sentence pairs
#   - Excellent at semantic similarity
#   - ~80MB download, cached locally after first use
#
# For code-heavy repos you can swap to:
#   "microsoft/codebert-base" (768 dims, much slower, better for code syntax)
#   "BAAI/bge-small-en-v1.5"  (384 dims, strong general retrieval)
MODEL_NAME  = "all-MiniLM-L6-v2"
EMBED_DIM   = 384
BATCH_SIZE  = 32     # Process 32 chunks at once — balances speed vs memory
TOP_K       = 6      # Default number of chunks to retrieve per query


# ── Singleton model loader ────────────────────────────────────────────────────

class _ModelSingleton:
    """
    Ensures the transformer model is loaded exactly once per process.

    Loading SentenceTransformer takes ~2 seconds and ~500MB RAM.
    Reloading it on every request would be unacceptable.

    Pattern: Lazy singleton — loads only when first accessed.
    """
    _instance: Optional[SentenceTransformer] = None

    @classmethod
    def get(cls) -> SentenceTransformer:
        if cls._instance is None:
            logger.info(f"Loading embedding model: {MODEL_NAME}")
            cls._instance = SentenceTransformer(MODEL_NAME)
            logger.info("Model loaded and cached.")
        return cls._instance


def get_model() -> SentenceTransformer:
    return _ModelSingleton.get()


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """
    One retrieved chunk with its similarity score.
    Returned by similarity_search().
    """
    content:     str
    file_path:   str
    language:    str
    start_line:  int
    end_line:    int
    node_name:   str
    node_type:   str
    score:       float      # cosine similarity: 0.0 (unrelated) → 1.0 (identical)
    chunk_index: int
    repo_id:     int

    def to_context_string(self) -> str:
        """
        Formats this result for injection into an LLM prompt.
        The format matches what the model expects as grounding context.
        """
        location = f"{self.file_path}"
        if self.node_name:
            location += f" → {self.node_type} `{self.node_name}`"
        location += f" (lines {self.start_line}–{self.end_line})"

        return (
            f"### Source: {location}\n"
            f"```{self.language.lower()}\n"
            f"{self.content}\n"
            f"```\n"
        )


# ── Index paths ───────────────────────────────────────────────────────────────

def _index_dir(repo_id: int) -> Path:
    path = settings.faiss_dir / f"repo_{repo_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path

def _index_path(repo_id: int) -> Path:
    return _index_dir(repo_id) / "index.faiss"

def _metadata_path(repo_id: int) -> Path:
    return _index_dir(repo_id) / "metadata.json"

def _chunks_path(repo_id: int) -> Path:
    return settings.faiss_dir / f"chunks_{repo_id}.json"


# ── Core embedding functions ──────────────────────────────────────────────────

def embed_texts(texts: List[str]) -> np.ndarray:
    """
    Embed a list of texts using the singleton model.

    Returns:
        np.ndarray of shape (len(texts), EMBED_DIM), dtype float32
        Vectors are L2-normalised (unit length).

    Why normalise?
        With unit vectors, inner product == cosine similarity.
        FAISS's IndexFlatIP (inner product) then gives us cosine
        similarity search — which is what we want for semantic search.
        Without normalisation, IndexFlatIP gives raw dot product,
        which is sensitive to vector magnitude (not just direction).
    """
    model = get_model()

    # show_progress_bar=False keeps logs clean during batch processing
    embeddings = model.encode(
        texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,   # L2 normalise in one step
    )
    return embeddings.astype(np.float32)


# ── Index builder ─────────────────────────────────────────────────────────────

class EmbedderService:
    """
    Builds, persists, and queries FAISS indexes per repository.
    """

    def index_exists(self, repo_id: int) -> bool:
        return _index_path(repo_id).exists()

    def build_index(self, repo_id: int) -> dict:
        """
        Full pipeline: load chunks → embed → build FAISS → save.

        Returns a summary dict with counts and timing.
        Raises FileNotFoundError if chunks_N.json doesn't exist.
        """
        import time
        t_start = time.time()

        chunks_file = _chunks_path(repo_id)
        if not chunks_file.exists():
            raise FileNotFoundError(
                f"No chunks file found for repo {repo_id}. "
                f"Run cloning and chunking first."
            )

        # ── Load chunks ───────────────────────────────────────────────────────
        chunks = json.loads(chunks_file.read_text(encoding="utf-8"))
        if not chunks:
            raise ValueError(f"Chunks file is empty for repo {repo_id}")

        logger.info(f"Embedding {len(chunks)} chunks for repo {repo_id}...")

        # ── Extract texts and metadata ────────────────────────────────────────
        # We embed document_text (content + header), not raw content.
        # The header ("File: auth.py | function: validate_token") gives
        # the embedding model important context about what the code IS.
        texts    = [c["document_text"] for c in chunks]
        metadata = [
            {k: v for k, v in c.items() if k != "document_text"}
            for c in chunks
        ]

        # ── Generate embeddings in batches ────────────────────────────────────
        # We process all texts at once here; embed_texts handles batching
        # internally via SentenceTransformer's batch_size parameter.
        all_embeddings = embed_texts(texts)
        # Shape: (num_chunks, 384) — one row per chunk

        # ── Build FAISS index ─────────────────────────────────────────────────
        #
        # Index type choice:
        #   IndexFlatIP  — exact search, inner product
        #                  Best for < 100k vectors (our use case)
        #                  No approximation error — perfect recall
        #
        #   IndexIVFFlat — approximate, needs training, faster for > 1M vectors
        #   IndexHNSWFlat— graph-based approx, good for > 500k vectors
        #
        # For a repo of 50k lines → ~2000 chunks → IndexFlatIP is perfect.
        index = faiss.IndexFlatIP(EMBED_DIM)

        # Add all vectors at once
        # FAISS assigns IDs 0, 1, 2, ... automatically (matches metadata list)
        index.add(all_embeddings)

        logger.info(f"FAISS index built: {index.ntotal} vectors")

        # ── Persist to disk ───────────────────────────────────────────────────
        faiss.write_index(index, str(_index_path(repo_id)))
        _metadata_path(repo_id).write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

        elapsed = round(time.time() - t_start, 2)
        summary = {
            "repo_id":    repo_id,
            "chunks":     len(chunks),
            "vectors":    index.ntotal,
            "dimensions": EMBED_DIM,
            "elapsed_s":  elapsed,
        }
        logger.info(f"Indexing complete: {summary}")
        return summary

    def load_index(self, repo_id: int) -> tuple[faiss.Index, list]:
        """
        Load a persisted FAISS index and its metadata from disk.

        Returns:
            (faiss_index, metadata_list)
            metadata_list[i] corresponds to vector i in the index.

        Raises:
            FileNotFoundError if index hasn't been built yet.
        """
        idx_path  = _index_path(repo_id)
        meta_path = _metadata_path(repo_id)

        if not idx_path.exists():
            raise FileNotFoundError(
                f"FAISS index not found for repo {repo_id}. "
                f"Call build_index() first."
            )

        index    = faiss.read_index(str(idx_path))
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))

        logger.debug(f"Loaded FAISS index: {index.ntotal} vectors for repo {repo_id}")
        return index, metadata
    
    def similarity_search(
        self,
        repo_id: int,
        query: str,
        k: int = 5,  # Ensure TOP_K is defined or replaced with a default
        score_threshold: float = 0.3,
    ) -> List[SearchResult]:
        """
        Find the k most semantically similar chunks to `query`.
        """
        # 1. PRE-CHECK: Ensure index exists and is loaded
        if not self.index_exists(repo_id):
            logger.warning(f"No index found for repo {repo_id}. Build it first.")
            return []
            
        # 2. LOAD: Ensure memory state is populated
        index, metadata = self.load_index(repo_id)

        # 3. EMBED: Use same normalization as build_index
        # Note: Ensure embed_texts returns a numpy array of type float32 for FAISS
        query_vec = embed_texts([query]) 

        # 4. SEARCH: Run FAISS inner-product search
        # scores: similarity, ids: vector indices
        scores, ids = index.search(query_vec, k)

        results: List[SearchResult] = []
        for score, idx in zip(scores[0], ids[0]):
            # FAISS returns -1 for "no result" padding
            if idx == -1:
                continue
            
            # Apply threshold
            if float(score) < score_threshold:
                continue

            # Map index back to metadata
            meta = metadata[idx]
            results.append(SearchResult(
                content     = meta.get("content", ""),
                file_path   = meta.get("file_path", ""),
                language    = meta.get("language", ""),
                start_line  = int(meta.get("start_line", 0)),
                end_line    = int(meta.get("end_line", 0)),
                node_name   = meta.get("node_name", ""),
                node_type   = meta.get("node_type", "chunk"),
                score       = float(score),
                chunk_index = int(meta.get("chunk_index", idx)),
                repo_id     = int(meta.get("repo_id", repo_id)),
            ))

        return results

    def delete_index(self, repo_id: int) -> None:
        """Remove all FAISS data for a repository."""
        import shutil
        index_dir = _index_dir(repo_id)
        if index_dir.exists():
            shutil.rmtree(index_dir)
        chunks_file = _chunks_path(repo_id)
        if chunks_file.exists():
            chunks_file.unlink()
        logger.info(f"Deleted FAISS index for repo {repo_id}")