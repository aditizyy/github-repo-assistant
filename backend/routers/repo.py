"""
repo.py — REST API endpoints for repository CRUD + cloning trigger.

Endpoints:
  POST   /api/repos/           — submit a new repo URL
  GET    /api/repos/           — list all repos
  GET    /api/repos/{id}       — get one repo + its files
  GET    /api/repos/{id}/chunks — preview structural chunk output
  GET    /api/repos/{id}/files  — list files for a repo
  POST   /api/repos/{id}/index  — trigger vector embeddings compilation
  GET    /api/repos/{id}/search — run vector similarity search over code
  DELETE /api/repos/{id}       — remove repo, files, and DB records
"""

import re
import json
import shutil
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator

from backend.database import get_db
from backend.models import Repository, IndexedFile
from backend.services.cloner import ClonerService
from backend.config import settings
from backend.services.chunker import CodeChunker
from backend.services.embedder import EmbedderService

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Pydantic schemas ─────────────────────────────────────────────────────────

GITHUB_URL_PATTERN = re.compile(
    r"^https?://github\.com/[\w.\-]+/[\w.\-]+(\.git)?$"
)


class RepoSubmit(BaseModel):
    github_url: str

    @field_validator("github_url")
    @classmethod
    def validate_github_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not GITHUB_URL_PATTERN.match(v):
            raise ValueError(
                "Must be a valid GitHub URL: https://github.com/owner/repo"
            )
        return v


class RepoResponse(BaseModel):
    id: int
    github_url: str
    repo_name: str
    status: str
    file_count: int
    chunk_count: int
    error_message: str | None

    class Config:
        from_attributes = True


class FileResponse(BaseModel):
    id: int
    file_path: str
    language: str | None
    line_count: int
    chunk_count: int

    class Config:
        from_attributes = True


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_repo_name(github_url: str) -> str:
    """Pulls 'owner/repo' from a GitHub URL."""
    # NOTE: do NOT use .rstrip(".git") here — rstrip treats its argument as a
    # set of characters to strip, not a literal suffix. It will eat any
    # trailing combination of '.', 'g', 'i', 't' — which silently chopped the
    # final 'i' off "fastapi" (→ "fastap"). removesuffix() strips the exact
    # literal string instead.
    cleaned = github_url.rstrip("/").removesuffix(".git")
    parts   = cleaned.split("/")
    return f"{parts[-2]}/{parts[-1]}"


# ── Route handlers ────────────────────────────────────────────────────────────

@router.post("/", response_model=RepoResponse, status_code=status.HTTP_202_ACCEPTED)
def submit_repository(
    payload: RepoSubmit,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Submit a GitHub URL for cloning and indexing.
    """
    url = payload.github_url

    existing = db.query(Repository).filter(Repository.github_url == url).first()
    if existing:
        if existing.status in ("ready", "indexing", "cloning"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Repository already exists with status: {existing.status}"
            )
        db.delete(existing)
        db.commit()

    repo = Repository(
        github_url=url,
        repo_name=extract_repo_name(url),
        status="pending",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)

    background_tasks.add_task(
        _run_clone_pipeline, repo.id
    )

    return repo


def _run_clone_pipeline(repo_id: int):
    """Background task wrapper."""
    from backend.database import SessionLocal
    db = SessionLocal()
    try:
        service = ClonerService(db)
        service.clone_and_index(repo_id)
    finally:
        db.close()


@router.get("/", response_model=list[RepoResponse])
def list_repositories(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """List all repositories, newest first."""
    return (
        db.query(Repository)
        .order_by(Repository.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{repo_id}", response_model=RepoResponse)
def get_repository(repo_id: int, db: Session = Depends(get_db)):
    """Get a single repository by ID. Used for status polling."""
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


@router.get("/{repo_id}/chunks")
def preview_chunks(
    repo_id: int,
    file_path: str | None = None,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    """
    Preview chunks for a repository.
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    chunks_path = settings.faiss_dir / f"chunks_{repo_id}.json"
    if not chunks_path.exists():
        raise HTTPException(status_code=404, detail="No chunks found. Re-index this repo.")

    chunks = json.loads(chunks_path.read_text())

    if file_path:
        chunks = [c for c in chunks if file_path in c["file_path"]]

    return {
        "total": len(chunks),
        "showing": min(limit, len(chunks)),
        "chunks": chunks[:limit],
    }


@router.get("/{repo_id}/files", response_model=list[FileResponse])
def list_files(
    repo_id: int,
    language: str | None = None,
    db: Session = Depends(get_db),
):
    """
    List all indexed files for a repository.
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    if repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_425_TOO_EARLY,
            detail=f"Repository is not ready yet (status: {repo.status})"
        )

    query = db.query(IndexedFile).filter(IndexedFile.repo_id == repo_id)
    if language:
        query = query.filter(IndexedFile.language == language)

    return query.order_by(IndexedFile.file_path).all()


@router.post("/{repo_id}/index")
def build_faiss_index(
    repo_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger FAISS index building for a repository.
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    if repo.status != "ready":
        raise HTTPException(
            status_code=400,
            detail=f"Repository must be 'ready' before indexing. Current: {repo.status}"
        )

    background_tasks.add_task(_run_embedding_pipeline, repo_id)
    return {"message": f"Embedding pipeline started for repo {repo_id}"}


def _run_embedding_pipeline(repo_id: int):
    """Background task wrapper — creates its own DB session."""
    from backend.database import SessionLocal
    db = SessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.id == repo_id).first()
        if repo:
            repo.status = "indexing"
            db.commit()

        service = EmbedderService()
        summary = service.build_index(repo_id)

        if repo:
            repo.status = "ready"
            db.commit()

        logger.info(f"Embedding complete: {summary}")
    except Exception as e:
        logger.exception(f"Embedding failed for repo {repo_id}: {e}")
        repo = db.query(Repository).filter(Repository.id == repo_id).first()
        if repo:
            repo.status = "error"
            repo.error_message = str(e)[:500]
            db.commit()
    finally:
        db.close()


@router.get("/{repo_id}/search")

def semantic_search(
    repo_id: int,
    q: str,
    k: int = 6,
    threshold: float = 0.3,
    db: Session = Depends(get_db),
):
    """
    Semantic search over a repository's codebase.
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    embedder = EmbedderService()
    if not embedder.index_exists(repo_id):
        raise HTTPException(
            status_code=404,
            detail="No FAISS index found. Call POST /api/repos/{id}/index first."
        )

    results = embedder.similarity_search(repo_id, q, k=k, score_threshold=threshold)

    return {
        "query": q,
        "results": [
            {
                "file_path":  r.file_path,
                "node_name":  r.node_name,
                "node_type":  r.node_type,
                "start_line": r.start_line,
                "end_line":   r.end_line,
                "language":   r.language,
                "score":      round(r.score, 4),
                "preview":    r.content[:200],
            }
            for r in results
        ],
    }


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repository(repo_id: int, db: Session = Depends(get_db)):
    """
    Delete repository record, all associated data, and the cloned files from disk.
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    if repo.clone_path:
        clone_dir = Path(repo.clone_path)
        if clone_dir.exists():
            shutil.rmtree(clone_dir)

    faiss_path = settings.faiss_dir / f"repo_{repo_id}"
    if faiss_path.exists():
        shutil.rmtree(faiss_path)

    db.delete(repo)
    db.commit()