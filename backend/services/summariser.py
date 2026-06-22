"""
summariser.py — Repository summary and documentation generation service.

Key engineering decisions:
  - Intelligent file sampling (not random) to maximise context quality
  - ASCII tree builder using a trie + DFS — classic DSA application
  - Prompt chaining: each step builds on previous output
  - All outputs cached to disk (expensive LLM calls shouldn't repeat)

IMPORTANT: this file uses gemini-2.5-flash, not gemini-1.5-flash.
The 1.5 line was retired from the API — see ai/chains.py for the same fix
applied to the chat pipeline. If you ever see "404 NotFound" or endless
"Retrying..." log lines, check the model name here first.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage

from backend.config import settings
from backend.models import Repository, IndexedFile
from ai.prompts import (
    REPO_SUMMARY_PROMPT, README_PROMPT,
    API_DOC_PROMPT, FUNCTION_DOC_PROMPT, ARCHITECTURE_PROMPT,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash"

# Files that are almost always entry points or architecturally important
PRIORITY_FILES = {
    "main.py", "app.py", "server.py", "index.py", "run.py",
    "manage.py", "__init__.py", "settings.py", "config.py",
    "index.js", "index.ts", "app.js", "app.ts", "server.js",
    "main.go", "main.rs", "Main.java", "Program.cs",
    "Dockerfile", "docker-compose.yml", "package.json",
    "pyproject.toml", "requirements.txt", "pom.xml", "build.gradle",
    "README.md", "schema.sql", ".env.example",
}

# API-related patterns — prioritised for API doc generation
API_PATTERNS = {
    "router", "routes", "views", "controllers", "api",
    "endpoints", "handlers", "resources",
}

MAX_SAMPLE_FILES   = 12    # cap LLM context size
MAX_FILE_CHARS     = 3000  # truncate each sampled file
MAX_TOTAL_CHARS    = 28000 # total context budget


# ── ASCII tree builder ────────────────────────────────────────────────────────

def build_file_tree(file_paths: list[str], max_depth: int = 4) -> str:
    """
    Build a formatted ASCII file tree from a flat list of relative paths.

    Algorithm: Trie (prefix tree) construction + DFS traversal.

    Example output:
        backend/
        ├── main.py
        ├── config.py
        └── routers/
            ├── chat.py
            └── repo.py

    Time complexity:  O(n * m) where n = files, m = max path depth
    Space complexity: O(n * m) for the trie nodes
    """
    root: dict = {}

    for path_str in sorted(file_paths):
        parts = Path(path_str).parts
        if len(parts) > max_depth:
            parts = parts[:max_depth] + ("...",)

        node = root
        for part in parts[:-1]:
            if part not in node:
                node[part] = {}
            node = node[part]

        filename = parts[-1]
        node.setdefault("__files__", []).append(filename)

    lines: list[str] = []

    def dfs(node: dict, prefix: str, name: str):
        lines.append(f"{prefix}{name}/")
        child_prefix = prefix + "│   "

        subdirs = sorted(k for k in node if k != "__files__")
        files   = sorted(node.get("__files__", []))
        items   = subdirs + files
        total   = len(items)

        for i, item in enumerate(items):
            connector = "└── " if i == total - 1 else "├── "
            if item in subdirs:
                new_prefix = prefix + ("    " if i == total - 1 else "│   ")
                dfs(node[item], new_prefix, item)
            else:
                lines.append(f"{child_prefix[:-4]}{connector}{item}")

    subdirs = sorted(k for k in root if k != "__files__")
    files   = sorted(root.get("__files__", []))
    total   = len(subdirs) + len(files)

    for i, item in enumerate(subdirs + files):
        connector = "└── " if i == total - 1 else "├── "
        if item in subdirs:
            prefix = "    " if i == total - 1 else "│   "
            dfs(root[item], prefix, item)
        else:
            lines.append(f"{connector}{item}")

    return "\n".join(lines)


# ── Intelligent file sampler ──────────────────────────────────────────────────

class FileSampler:
    """
    Selects the most architecturally informative files for LLM context.

    Strategy (priority order):
      1. Priority files (entry points, config, package manifests)
      2. API/router files (highest information density for understanding)
      3. Source code over config/docs
      4. Largest files by line count (more code = more information)
    """

    def __init__(self, clone_path: Path):
        self.clone_path = clone_path

    def score_file(self, file_rec) -> int:
        score  = 0
        name   = Path(file_rec.file_path).name.lower()
        parts  = {p.lower() for p in Path(file_rec.file_path).parts}

        if Path(file_rec.file_path).name in PRIORITY_FILES:
            score += 100

        if parts & API_PATTERNS:
            score += 50

        if file_rec.language in ("Python", "JavaScript", "TypeScript", "Go", "Java"):
            score += 30

        score += min((file_rec.line_count or 0) // 10, 40)

        if "test" in name or "spec" in name or "test" in parts:
            score -= 20

        return score

    def sample(
        self,
        file_records: list,
        max_files: int = MAX_SAMPLE_FILES,
    ) -> list[tuple[str, str, str]]:
        """Returns list of (file_path, content, language) tuples."""
        scored   = sorted(file_records, key=self.score_file, reverse=True)
        selected = scored[:max_files]

        results: list[tuple[str, str, str]] = []
        total_chars = 0

        for rec in selected:
            full_path = self.clone_path / rec.file_path
            if not full_path.exists():
                continue

            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining < 200:
                break

            truncated = content[:min(MAX_FILE_CHARS, remaining)]
            if len(content) > MAX_FILE_CHARS:
                truncated += f"\n... [truncated, {len(content)} chars total]"

            results.append((rec.file_path, truncated, rec.language or "text"))
            total_chars += len(truncated)

        return results


def format_samples(samples: list[tuple[str, str, str]]) -> str:
    """Format file samples for prompt injection."""
    parts = []
    for file_path, content, language in samples:
        lang_lower = (language or "text").lower().replace(" ", "")
        parts.append(
            f"### File: `{file_path}` ({language})\n"
            f"```{lang_lower}\n{content}\n```"
        )
    return "\n\n".join(parts)


# ── LLM caller ───────────────────────────────────────────────────────────────

def _call_llm(prompt: str, temperature: float = 0.2) -> str:
    """Single LLM call. Centralised for easy model swapping."""
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=settings.gemini_api_key,
        temperature=temperature,
        convert_system_message_to_human=True,
    )
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content.strip()


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_dir(repo_id: int) -> Path:
    path = settings.faiss_dir / f"docs_{repo_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached(repo_id: int, key: str) -> Optional[str]:
    path = _cache_dir(repo_id) / f"{key}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _save_cache(repo_id: int, key: str, content: str) -> None:
    path = _cache_dir(repo_id) / f"{key}.txt"
    path.write_text(content, encoding="utf-8")


# ── Main summariser service ───────────────────────────────────────────────────

class SummariserService:
    """
    Orchestrates multi-step documentation generation.
    Results are cached to disk — re-calling is cheap after the first run.
    """

    def __init__(self, db: Session):
        self.db = db

    def _load_repo(self, repo_id: int) -> tuple[Repository, Path, list]:
        repo = self.db.query(Repository).filter(Repository.id == repo_id).first()
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")
        if repo.status != "ready":
            raise ValueError(f"Repository not ready: {repo.status}")
        if not repo.clone_path:
            raise ValueError(f"Repository {repo_id} has no clone_path recorded")

        clone_path   = Path(repo.clone_path)
        file_records = (
            self.db.query(IndexedFile)
            .filter(IndexedFile.repo_id == repo_id)
            .all()
        )
        return repo, clone_path, file_records

    # ── 1. Project summary ────────────────────────────────────────────────────

    def get_summary(self, repo_id: int, force: bool = False) -> dict:
        cache_key = "summary"
        if not force:
            cached = _cached(repo_id, cache_key)
            if cached:
                return json.loads(cached)

        repo, clone_path, file_records = self._load_repo(repo_id)

        file_paths  = [r.file_path for r in file_records]
        file_tree   = build_file_tree(file_paths)
        sampler     = FileSampler(clone_path)
        samples     = sampler.sample(file_records)
        samples_str = format_samples(samples)

        prompt = REPO_SUMMARY_PROMPT.format(
            repo_name=repo.repo_name,
            file_tree=file_tree,
            samples=samples_str,
        )

        logger.info(f"Generating summary for repo {repo_id}...")
        raw = _call_llm(prompt, temperature=0.1)

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]

        try:
            summary = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Summary JSON parse failed, returning raw text")
            summary = {"purpose": raw, "tech_stack": {}, "error": "parse_failed"}

        _save_cache(repo_id, cache_key, json.dumps(summary))
        return summary

    # ── 2. README generation ──────────────────────────────────────────────────

    def generate_readme(self, repo_id: int, force: bool = False) -> str:
        cache_key = "readme"
        if not force:
            cached = _cached(repo_id, cache_key)
            if cached:
                return cached

        repo, clone_path, file_records = self._load_repo(repo_id)
        summary = self.get_summary(repo_id)

        file_paths  = [r.file_path for r in file_records]
        file_tree   = build_file_tree(file_paths)
        sampler     = FileSampler(clone_path)
        samples     = sampler.sample(file_records)
        samples_str = format_samples(samples)

        prompt = README_PROMPT.format(
            summary=json.dumps(summary, indent=2),
            file_tree=file_tree,
            samples=samples_str,
        )

        logger.info(f"Generating README for repo {repo_id}...")
        readme = _call_llm(prompt, temperature=0.3)
        _save_cache(repo_id, cache_key, readme)
        return readme

    # ── 3. API documentation ──────────────────────────────────────────────────

    def generate_api_docs(self, repo_id: int, force: bool = False) -> str:
        cache_key = "api_docs"
        if not force:
            cached = _cached(repo_id, cache_key)
            if cached:
                return cached

        repo, clone_path, file_records = self._load_repo(repo_id)

        api_files = [
            r for r in file_records
            if any(pat in Path(r.file_path).parts for pat in API_PATTERNS)
            or any(pat in Path(r.file_path).stem.lower() for pat in API_PATTERNS)
        ]
        target_files = api_files if api_files else file_records

        sampler     = FileSampler(clone_path)
        samples     = sampler.sample(target_files, max_files=8)
        samples_str = format_samples(samples)

        prompt = API_DOC_PROMPT.format(samples=samples_str)

        logger.info(f"Generating API docs for repo {repo_id}...")
        api_docs = _call_llm(prompt, temperature=0.2)
        _save_cache(repo_id, cache_key, api_docs)
        return api_docs

    # ── 4. Function documentation ─────────────────────────────────────────────

    def generate_function_docs(
        self,
        repo_id: int,
        file_path: str,
        force: bool = False,
    ) -> str:
        safe_key  = file_path.replace("/", "_").replace(".", "_")
        cache_key = f"func_docs_{safe_key}"

        if not force:
            cached = _cached(repo_id, cache_key)
            if cached:
                return cached

        repo, clone_path, _ = self._load_repo(repo_id)

        full_path = clone_path / file_path
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        content  = full_path.read_text(encoding="utf-8", errors="ignore")
        language = _detect_language_from_path(file_path)

        if len(content) > 4000:
            content = content[:4000] + "\n\n... [file truncated for documentation]"

        prompt = FUNCTION_DOC_PROMPT.format(
            file_path=file_path,
            language=language,
            language_lower=language.lower(),
            content=content,
        )

        logger.info(f"Generating function docs for {file_path} in repo {repo_id}...")
        docs = _call_llm(prompt, temperature=0.2)
        _save_cache(repo_id, cache_key, docs)
        return docs

    # ── 5. Architecture overview ──────────────────────────────────────────────

    def generate_architecture(self, repo_id: int, force: bool = False) -> str:
        cache_key = "architecture"
        if not force:
            cached = _cached(repo_id, cache_key)
            if cached:
                return cached

        repo, clone_path, file_records = self._load_repo(repo_id)

        file_paths  = [r.file_path for r in file_records]
        file_tree   = build_file_tree(file_paths)
        sampler     = FileSampler(clone_path)
        samples     = sampler.sample(file_records, max_files=10)
        samples_str = format_samples(samples)

        prompt = ARCHITECTURE_PROMPT.format(
            repo_name=repo.repo_name,
            file_tree=file_tree,
            samples=samples_str,
        )

        logger.info(f"Generating architecture analysis for repo {repo_id}...")
        arch = _call_llm(prompt, temperature=0.2)
        _save_cache(repo_id, cache_key, arch)
        return arch

    # ── 6. Full doc bundle ────────────────────────────────────────────────────

    def generate_all(self, repo_id: int) -> dict:
        logger.info(f"Generating full documentation bundle for repo {repo_id}")
        return {
            "summary":      self.get_summary(repo_id),
            "readme":       self.generate_readme(repo_id),
            "api_docs":     self.generate_api_docs(repo_id),
            "architecture": self.generate_architecture(repo_id),
        }


def _detect_language_from_path(file_path: str) -> str:
    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".java": "Java", ".go": "Go", ".rs": "Rust",
        ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
        ".cpp": "C++", ".c": "C", ".kt": "Kotlin",
        ".html": "HTML", ".css": "CSS", ".md": "Markdown",
    }
    return ext_map.get(Path(file_path).suffix.lower(), "text")
