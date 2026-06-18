"""
cloner.py — Handles cloning a GitHub repo and extracting source files.

Design decisions:
  - Runs as a background task (non-blocking for the HTTP response)
  - Updates DB status at each stage so the frontend can poll progress
  - Aggressive filtering so we only store files the AI can actually use
"""

import os
import shutil
import logging
from pathlib import Path
from typing import Generator
from git import Repo, GitCommandError
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Repository, IndexedFile

from backend.services.chunker import CodeChunker, CodeChunk
from backend.services.embedder import EmbedderService

logger = logging.getLogger(__name__)

# ── Filtering configuration ──────────────────────────────────────────────────

# Directories that are never useful for code understanding
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", "out", "coverage",
    ".pytest_cache", ".mypy_cache", ".tox", "eggs", ".eggs",
    "htmlcov", ".cache", "vendor", "bower_components",
}

# Source code and config extensions we DO want
ALLOWED_EXTENSIONS = {
    # Python
    ".py", ".pyi",
    # JavaScript / TypeScript
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # Web
    ".html", ".css", ".scss", ".sass", ".less",
    # Backend languages
    ".java", ".kt", ".go", ".rs", ".cpp", ".c", ".h", ".cs", ".php", ".rb",
    # Config / data
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env.example",
    # Docs
    ".md", ".txt", ".rst",
    # SQL
    ".sql",
    # Shell
    ".sh", ".bash", ".zsh",
    # Docker / CI
    "Dockerfile", ".dockerignore", ".github",
}

# Hard size limit — files bigger than this are likely generated/minified
MAX_FILE_SIZE_BYTES = 500 * 1024  # 500 KB


def is_allowed_file(file_path: Path) -> bool:
    """
    Returns True if this file should be indexed.

    Checks:
      1. Not in an ignored directory
      2. Has an allowed extension (or is an allowed filename like Dockerfile)
      3. Not too large
      4. Is valid UTF-8 text (not a binary blob)
    """
    # Check every part of the path against ignored dirs
    for part in file_path.parts:
        if part in IGNORED_DIRS:
            return False

    # Check extension
    suffix = file_path.suffix.lower()
    name   = file_path.name

    # Allow files with allowed extensions OR exact filenames like Dockerfile
    if suffix not in ALLOWED_EXTENSIONS and name not in ALLOWED_EXTENSIONS:
        return False

    # Size check
    try:
        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            logger.debug(f"Skipping large file: {file_path}")
            return False
    except OSError:
        return False

    # Binary check — try reading a small chunk as UTF-8
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(1024)
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def detect_language(file_path: Path) -> str:
    """
    Maps file extension to a human-readable language name.
    Used for display in the frontend and for prompt context.
    """
    ext_map = {
        ".py": "Python", ".pyi": "Python",
        ".js": "JavaScript", ".jsx": "JavaScript",
        ".ts": "TypeScript", ".tsx": "TypeScript",
        ".java": "Java", ".kt": "Kotlin",
        ".go": "Go", ".rs": "Rust",
        ".cpp": "C++", ".c": "C", ".h": "C/C++ Header",
        ".cs": "C#", ".php": "PHP", ".rb": "Ruby",
        ".html": "HTML", ".css": "CSS",
        ".scss": "SCSS", ".sass": "Sass",
        ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
        ".toml": "TOML", ".md": "Markdown",
        ".sql": "SQL", ".sh": "Shell", ".bash": "Shell",
    }
    name_map = {
        "Dockerfile": "Docker", "Makefile": "Makefile",
    }
    return (
        name_map.get(file_path.name)
        or ext_map.get(file_path.suffix.lower(), "Text")
    )


def walk_repository(repo_path: Path) -> Generator[Path, None, None]:
    """
    Yields allowed file paths from a cloned repo.

    Uses os.walk for efficiency. We prune ignored dirs in-place
    (modifying dirs[:] tells os.walk not to recurse into them).
    This is a classic interview-worthy pattern — O(n) traversal,
    skipping entire subtrees without visiting them.
    """
    for root, dirs, files in os.walk(repo_path):
        # Prune ignored directories IN PLACE — prevents recursion into them
        # This is more efficient than checking paths after the fact
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        for filename in files:
            full_path = Path(root) / filename
            if is_allowed_file(full_path):
                yield full_path


def read_file_content(file_path: Path) -> str | None:
    """
    Reads file content safely. Returns None if unreadable.
    Always normalises line endings to \\n.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        return content.replace("\r\n", "\n").replace("\r", "\n")
    except Exception as e:
        logger.warning(f"Could not read {file_path}: {e}")
        return None


# ── Main cloning service ─────────────────────────────────────────────────────

class ClonerService:
    """
    Orchestrates: clone → walk → filter → persist metadata.
    One instance per cloning job is fine (stateless after __init__).
    """

    def __init__(self, db: Session):
        self.db = db

    def _update_status(self, repo: Repository, status: str, **kwargs):
        """Helper to update repo status and any extra fields atomically."""
        repo.status = status
        for key, value in kwargs.items():
            setattr(repo, key, value)
        self.db.commit()
        logger.info(f"Repo {repo.id} → status: {status}")

    def clone_and_index(self, repo_id: int) -> None:
        """
        Full pipeline for one repository.
        """
        repo = self.db.query(Repository).filter(Repository.id == repo_id).first()
        if not repo:
            logger.error(f"Repo ID {repo_id} not found in DB")
            return

        clone_path = settings.repos_dir / f"repo_{repo_id}"

        try:
            # ── Stage 1: Clone ──────────────────────────────────────────────
            self._update_status(repo, "cloning")
            if clone_path.exists():
                shutil.rmtree(clone_path)

            logger.info(f"Cloning {repo.github_url} → {clone_path}")
            Repo.clone_from(repo.github_url, clone_path, depth=1)
            self._update_status(repo, "indexing", clone_path=str(clone_path))

            # ── Stage 2: Walk & filter files ────────────────────────────────
            allowed_files = list(walk_repository(clone_path))
            
            # ── Stage 3: Chunk ──────────────────────────────────────────────
            self.db.query(IndexedFile).filter(IndexedFile.repo_id == repo_id).delete()
            chunker = CodeChunker()
            file_tuples = [(str(p), str(p.relative_to(clone_path)), detect_language(p)) for p in allowed_files]
            all_chunks = chunker.chunk_repository(repo_id, file_tuples)

            # ── CRITICAL: Save JSON to disk BEFORE calling embedder ─────────
            import json
            chunks_path = settings.faiss_dir / f"chunks_{repo_id}.json"
            chunks_data = [
                {**c.to_metadata(), "content": c.content, "document_text": c.to_document_text()}
                for c in all_chunks
            ]
            chunks_path.write_text(json.dumps(chunks_data, indent=2), encoding="utf-8")
            logger.info(f"Saved {len(all_chunks)} chunks → {chunks_path}")

            # ── Stage 4: Index & Persist DB ────────────────────────────────
            indexed_file_records = []
            chunk_counts = {c.file_path: 0 for c in all_chunks} # Simple map
            for c in all_chunks: chunk_counts[c.file_path] += 1

            for file_path in allowed_files:
                rel_path = str(file_path.relative_to(clone_path))
                content = read_file_content(file_path)
                indexed_file_records.append(IndexedFile(
                    repo_id=repo_id, file_path=rel_path,
                    language=detect_language(file_path),
                    line_count=content.count("\n") + 1 if content else 0,
                    chunk_count=chunk_counts.get(rel_path, 0),
                ))
            self.db.bulk_save_objects(indexed_file_records)

            embedder = EmbedderService()
            embedder.build_index(repo_id)

            self._update_status(repo, "ready", file_count=len(allowed_files), chunk_count=len(all_chunks))
            logger.info(f"Full pipeline complete for repo {repo_id}")

        except GitCommandError as e:
            self._update_status(repo, "error", error_message=str(e)[:500])
        except Exception as e:
            logger.exception(f"Unexpected error for repo {repo_id}: {e}")
            self._update_status(repo, "error", error_message=str(e)[:500])