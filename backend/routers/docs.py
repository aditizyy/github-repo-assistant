"""
docs.py — Documentation generation endpoints.

Endpoints:
  GET  /api/docs/{repo_id}/summary          — structured project summary
  GET  /api/docs/{repo_id}/readme           — full README.md
  GET  /api/docs/{repo_id}/api              — API documentation
  GET  /api/docs/{repo_id}/architecture     — architecture analysis
  GET  /api/docs/{repo_id}/file             — per-file function docs
  POST /api/docs/{repo_id}/generate-all     — generate everything at once
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.services.summariser import SummariserService

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_service(db: Session) -> SummariserService:
    return SummariserService(db)


@router.get("/{repo_id}/summary")
def get_summary(
    repo_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """
    Get structured project summary as JSON.
    ?force=true regenerates even if cached.
    """
    try:
        return _get_service(db).get_summary(repo_id, force=force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{repo_id}/readme", response_class=PlainTextResponse)
def get_readme(
    repo_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Generate a complete README.md. Returns raw markdown text."""
    try:
        return _get_service(db).generate_readme(repo_id, force=force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{repo_id}/api", response_class=PlainTextResponse)
def get_api_docs(
    repo_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Generate API endpoint documentation."""
    try:
        return _get_service(db).generate_api_docs(repo_id, force=force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{repo_id}/architecture", response_class=PlainTextResponse)
def get_architecture(
    repo_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Generate architecture analysis with Mermaid diagram."""
    try:
        return _get_service(db).generate_architecture(repo_id, force=force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{repo_id}/file", response_class=PlainTextResponse)
def get_file_docs(
    repo_id: int,
    file_path: str = Query(..., description="Relative file path, e.g. backend/auth.py"),
    force: bool = False,
    db: Session = Depends(get_db),
):
    """
    Generate documentation for all functions/classes in a specific file.
    Example: GET /api/docs/1/file?file_path=index.html
    """
    try:
        return _get_service(db).generate_function_docs(repo_id, file_path, force=force)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{repo_id}/generate-all")
def generate_all_docs(
    repo_id: int,
    background_tasks: BackgroundTasks,
):
    """
    Kick off full documentation generation as a background task.
    Returns immediately; poll the individual GET endpoints to check progress
    (each one returns from cache once ready).

    NOTE: deliberately does NOT take db: Session = Depends(get_db) — that
    session would be closed before the background task runs. The task opens
    its own session instead, same pattern as cloner.py and the /stream route.
    """
    def _run(repo_id: int):
        from backend.database import SessionLocal
        _db = SessionLocal()
        try:
            SummariserService(_db).generate_all(repo_id)
            logger.info(f"Full doc bundle complete for repo {repo_id}")
        except Exception as e:
            logger.exception(f"Doc generation failed for repo {repo_id}: {e}")
        finally:
            _db.close()

    background_tasks.add_task(_run, repo_id)
    return {"message": f"Documentation generation started for repo {repo_id}"}
