"""
analysis.py — Bug detection and security scanning endpoints.

Endpoints:
  POST /api/analysis/{repo_id}/scan      — run full repo scan (background task)
  GET  /api/analysis/{repo_id}/results   — get cached scan results (filterable)
  GET  /api/analysis/{repo_id}/summary   — health score + counts only (fast)
  GET  /api/analysis/{repo_id}/file      — analyse a single file on demand
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.services.analyser import AnalysisService, _load_cache

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{repo_id}/scan")
def trigger_scan(
    repo_id: int,
    background_tasks: BackgroundTasks,
    force: bool = False,
):
    """
    Trigger a full repository security + bug scan as a background task.
    Returns immediately. Poll GET /api/analysis/{repo_id}/results afterwards.

    NOTE: no db: Session = Depends(get_db) here — the background task opens
    its own SessionLocal(), same pattern used everywhere else in this project
    for tasks that outlive the original HTTP request.
    """
    def _run(repo_id: int, force: bool):
        from backend.database import SessionLocal
        _db = SessionLocal()
        try:
            AnalysisService(_db).analyse_repository(repo_id, force=force)
        except Exception as e:
            logger.exception(f"Analysis failed for repo {repo_id}: {e}")
        finally:
            _db.close()

    background_tasks.add_task(_run, repo_id, force)
    return {"message": f"Analysis started for repo {repo_id}"}


@router.get("/{repo_id}/results")
def get_results(
    repo_id: int,
    severity: str | None = Query(None, description="Filter: critical|high|medium|low|info"),
    category: str | None = Query(None, description="Filter: security|bug|complexity|style|debt"),
):
    """Get cached analysis results, optionally filtered by severity or category."""
    data = _load_cache(repo_id)
    if not data:
        raise HTTPException(
            status_code=404,
            detail="No analysis results found. Run POST /api/analysis/{id}/scan first."
        )

    issues = data["issues"]
    if severity:
        issues = [i for i in issues if i["severity"] == severity]
    if category:
        issues = [i for i in issues if i["category"] == category]

    return {**data, "issues": issues}


@router.get("/{repo_id}/file")
def analyse_file(
    repo_id: int,
    file_path: str = Query(..., description="Relative file path"),
    db: Session = Depends(get_db),
):
    """Analyse a single file on demand — not cached, always fresh."""
    try:
        return AnalysisService(db).analyse_file(repo_id, file_path)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{repo_id}/summary")
def get_summary(repo_id: int):
    """Summary stats only (no issue list) — fast for dashboards."""
    data = _load_cache(repo_id)
    if not data:
        raise HTTPException(status_code=404, detail="Run a scan first.")

    return {
        "repo_id":           data["repo_id"],
        "repo_name":         data["repo_name"],
        "files_scanned":     data["files_scanned"],
        "files_with_issues": data["files_with_issues"],
        "total_issues":      data["total_issues"],
        "severity_counts":   data["severity_counts"],
        "category_counts":   data["category_counts"],
        "health_score":      data["health_score"],
    }
