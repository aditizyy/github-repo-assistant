"""
Test the embedding pipeline end-to-end without a real repository.
Run with: python -m backend.services.embedder_test
"""
import numpy as np
from backend.services.embedder import embed_texts, EmbedderService, SearchResult

def test_embedding_shape():
    texts = [
        "def authenticate_user(token: str): ...",
        "SELECT * FROM users WHERE id = ?",
        "import React from 'react'",
    ]
    embeddings = embed_texts(texts)
    assert embeddings.shape == (3, 384), f"Expected (3, 384), got {embeddings.shape}"
    print(f"✅ Embedding shape correct: {embeddings.shape}")


def test_normalisation():
    texts = ["hello world"]
    vec = embed_texts(texts)[0]
    norm = np.linalg.norm(vec)
    assert abs(norm - 1.0) < 1e-5, f"Vector not normalised: norm={norm}"
    print(f"✅ L2 norm = {norm:.6f} (should be ~1.0)")


def test_semantic_similarity():
    """
    Similar texts should have high cosine similarity.
    Dissimilar texts should have low cosine similarity.
    With unit vectors, dot product = cosine similarity.
    """
    vecs = embed_texts([
        "def validate_jwt_token(token):",   # query
        "def verify_token(jwt_string):",    # semantically close
        "def calculate_fibonacci(n):",      # semantically far
    ])

    sim_close = float(np.dot(vecs[0], vecs[1]))
    sim_far   = float(np.dot(vecs[0], vecs[2]))

    print(f"  Similarity (validate_jwt vs verify_token):   {sim_close:.4f}")
    print(f"  Similarity (validate_jwt vs fibonacci):      {sim_far:.4f}")

    assert sim_close > sim_far, "Semantic similarity ordering is wrong!"
    print("✅ Semantic similarity ordering is correct")


def test_full_pipeline():
    """
    Write fake chunks, build a FAISS index, run a search.
    Tests the full embed → index → search loop.
    """
    import json, tempfile
    from pathlib import Path
    from unittest.mock import patch

    fake_chunks = [
        {
            "content":       "def validate_token(token: str):\n    payload = jwt.decode(token, SECRET)\n    return payload",
            "document_text": "# File: auth.py | function: validate_token | Lines: 10–14\n\ndef validate_token(token: str):\n    payload = jwt.decode(token, SECRET)\n    return payload",
            "file_path":     "auth.py",
            "language":      "Python",
            "start_line":    "10",
            "end_line":      "14",
            "chunk_index":   "0",
            "node_name":     "validate_token",
            "node_type":     "function",
            "repo_id":       "999",
        },
        {
            "content":       "def get_user(user_id: int):\n    return db.query(User).filter(User.id == user_id).first()",
            "document_text": "# File: users.py | function: get_user | Lines: 5–7\n\ndef get_user(user_id: int):\n    return db.query(User).filter(User.id == user_id).first()",
            "file_path":     "users.py",
            "language":      "Python",
            "start_line":    "5",
            "end_line":      "7",
            "chunk_index":   "1",
            "node_name":     "get_user",
            "node_type":     "function",
            "repo_id":       "999",
        },
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Patch storage paths to use temp dir
        chunks_file = tmp / "chunks_999.json"
        chunks_file.write_text(json.dumps(fake_chunks))

        with patch("backend.services.embedder.settings") as mock_settings:
            mock_settings.faiss_dir = tmp

            service = EmbedderService()
            summary = service.build_index(999)
            print(f"  Build summary: {summary}")

            results = service.similarity_search(
                999, "where is JWT token validated?", k=2
            )

            print(f"  Top result: {results[0].file_path} "
                  f"({results[0].node_name}) — score {results[0].score:.4f}")
            assert results[0].file_path == "auth.py", \
                f"Expected auth.py first, got {results[0].file_path}"
            print("✅ Semantic search returned correct result first")


if __name__ == "__main__":
    print("\n── Embedding Pipeline Tests ──────────────────────────\n")
    test_embedding_shape()
    test_normalisation()
    test_semantic_similarity()
    test_full_pipeline()
    print("\n── All tests passed ✅ ───────────────────────────────\n")