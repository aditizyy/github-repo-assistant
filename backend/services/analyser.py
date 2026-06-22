"""
analyser.py — Orchestrates bug detection + security scanning across a repo.

Adds LLM-powered explanations for the top critical/high issues found.
Results are cached to disk — scanning is real work, re-scanning identical
code is wasteful.

Uses gemini-2.5-flash (same model fix applied across the whole project —
see ai/chains.py and backend/services/summariser.py for the same change).
"""

import json
import logging
from pathlib import Path
from typing import List
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage

from backend.config import settings
from backend.models import Repository, IndexedFile
from backend.services.bug_detector import PythonBugDetector, Issue
from backend.services.security_scanner import SecurityScanner

logger = logging.getLogger(__name__)

GEMINI_MODEL       = "gemini-2.5-flash"
PYTHON_EXTENSIONS  = {".py", ".pyi"}
MAX_LLM_ISSUES     = 5
CACHE_FILENAME     = "analysis_results.json"


def _cache_path(repo_id: int) -> Path:
    path = settings.faiss_dir / f"analysis_{repo_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path / CACHE_FILENAME


def _load_cache(repo_id: int) -> dict | None:
    p = _cache_path(repo_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save_cache(repo_id: int, data: dict) -> None:
    _cache_path(repo_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── LLM enhancer ─────────────────────────────────────────────────────────────

def _llm_enhance_issues(issues: List[Issue]) -> list[dict]:
    """
    For the top critical/high issues, ask Gemini for a deeper explanation
    and a concrete fix. Batches into ONE call rather than one call per issue.
    """
    if not issues:
        return []

    top_issues = [i for i in issues if i.severity in ("critical", "high")][:MAX_LLM_ISSUES]
    if not top_issues:
        top_issues = issues[:MAX_LLM_ISSUES]
    if not top_issues:
        return []

    issues_text = "\n\n".join([
        f"Issue {idx + 1}:\n"
        f"  File: {i.file_path}, Line: {i.line}\n"
        f"  Rule: {i.rule}\n"
        f"  Title: {i.title}\n"
        f"  Code: {i.snippet}"
        for idx, i in enumerate(top_issues)
    ])

    prompt = f"""You are a senior security engineer reviewing code issues.
For each issue below, provide:
1. Why this is dangerous (1-2 sentences, specific to the code shown)
2. Exact code fix (before/after code snippet)
3. Risk level in context (explain impact if exploited)

Issues to review:
{issues_text}

Respond in this exact format for each issue:
### Issue N
**Why dangerous:** ...
**Fix:**
```
# Before
...
# After
...
```
**Risk:** ...
"""

    try:
        llm = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            google_api_key=settings.gemini_api_key,
            temperature=0.1,
            convert_system_message_to_human=True,
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        enhanced_text = response.content

        enhanced = []
        for i, issue in enumerate(top_issues):
            d = issue.to_dict()
            d["llm_analysis"] = _extract_llm_section(enhanced_text, i + 1)
            enhanced.append(d)
        return enhanced

    except Exception as e:
        logger.warning(f"LLM enhancement failed: {e}")
        return [i.to_dict() for i in top_issues]


def _extract_llm_section(text: str, issue_num: int) -> str:
    marker      = f"### Issue {issue_num}"
    next_marker = f"### Issue {issue_num + 1}"

    start = text.find(marker)
    if start == -1:
        return ""

    end = text.find(next_marker, start)
    return text[start:end if end != -1 else len(text)].strip()


# ── Main analysis orchestrator ────────────────────────────────────────────────

class AnalysisService:
    """
    Full-repo analysis pipeline:
      1. Walk all indexed files
      2. Run PythonBugDetector on .py files
      3. Run SecurityScanner on all files
      4. Deduplicate issues
      5. Run LLM enhancement on top issues
      6. Cache results
    """

    def __init__(self, db: Session):
        self.db               = db
        self.bug_detector     = PythonBugDetector()
        self.security_scanner = SecurityScanner()

    def analyse_repository(self, repo_id: int, force: bool = False) -> dict:
        if not force:
            cached = _load_cache(repo_id)
            if cached:
                logger.info(f"Returning cached analysis for repo {repo_id}")
                return cached

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

        all_issues:        List[Issue] = []
        files_with_issues: set[str]   = set()
        files_scanned      = 0

        logger.info(f"Starting analysis of {len(file_records)} files in repo {repo_id}")

        for rec in file_records:
            full_path = clone_path / rec.file_path
            if not full_path.exists():
                continue

            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            files_scanned += 1
            file_issues: List[Issue] = []

            ext = Path(rec.file_path).suffix.lower()
            if ext in PYTHON_EXTENSIONS:
                file_issues.extend(self.bug_detector.analyse(content, rec.file_path))

            file_issues.extend(self.security_scanner.scan(content, rec.file_path))

            if file_issues:
                files_with_issues.add(rec.file_path)

            all_issues.extend(file_issues)

        # Deduplicate on (file, line, rule)
        seen = set()
        unique_issues: List[Issue] = []
        for issue in all_issues:
            key = (issue.file_path, issue.line, issue.rule)
            if key not in seen:
                seen.add(key)
                unique_issues.append(issue)

        SORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        unique_issues.sort(key=lambda i: (SORDER.get(i.severity, 99), i.file_path, i.line))

        severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        for issue in unique_issues:
            severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1

        category_counts: dict[str, int] = {}
        for issue in unique_issues:
            category_counts[issue.category] = category_counts.get(issue.category, 0) + 1

        logger.info("Running LLM enhancement on critical issues...")
        enhanced = _llm_enhance_issues(unique_issues)

        result = {
            "repo_id":           repo_id,
            "repo_name":         repo.repo_name,
            "files_scanned":     files_scanned,
            "files_with_issues": len(files_with_issues),
            "total_issues":      len(unique_issues),
            "severity_counts":   severity_counts,
            "category_counts":   category_counts,
            "health_score":      _compute_health_score(severity_counts, files_scanned),
            "issues":            [i.to_dict() for i in unique_issues],
            "enhanced_issues":   enhanced,
        }

        _save_cache(repo_id, result)
        logger.info(f"Analysis complete: {len(unique_issues)} issues in {files_scanned} files")
        return result

    def analyse_file(self, repo_id: int, file_path: str) -> dict:
        """Analyse a single file on demand. Not cached — always fresh."""
        repo = self.db.query(Repository).filter(Repository.id == repo_id).first()
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")
        if not repo.clone_path:
            raise ValueError(f"Repository {repo_id} has no clone_path recorded")

        full_path = Path(repo.clone_path) / file_path
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        content = full_path.read_text(encoding="utf-8", errors="ignore")
        issues: List[Issue] = []

        ext = full_path.suffix.lower()
        if ext in PYTHON_EXTENSIONS:
            issues.extend(self.bug_detector.analyse(content, file_path))

        issues.extend(self.security_scanner.scan(content, file_path))

        SORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        issues.sort(key=lambda i: SORDER.get(i.severity, 99))

        return {
            "file_path":    file_path,
            "total_issues": len(issues),
            "issues":       [i.to_dict() for i in issues],
        }


def _compute_health_score(severity_counts: dict, files_scanned: int) -> int:
    """
    0–100 health score. Penalty weights:
      critical: -15, high: -8, medium: -3, low: -1 (per occurrence),
      normalised by file count so large repos aren't unfairly penalised.
    """
    if files_scanned == 0:
        return 100

    penalty = (
        severity_counts.get("critical", 0) * 15 +
        severity_counts.get("high",     0) * 8  +
        severity_counts.get("medium",   0) * 3  +
        severity_counts.get("low",      0) * 1
    )

    normalised_penalty = (penalty / files_scanned) * 5
    return max(0, min(100, round(100 - normalised_penalty)))
