"""
chat.py — Chat API endpoints.

Endpoints:
  POST /api/chat/{repo_id}/message     — non-streaming chat
  POST /api/chat/{repo_id}/stream      — streaming chat (SSE)
  GET  /api/chat/{repo_id}/sessions    — list sessions for a repo
  GET  /api/chat/sessions/{id}/history — full message history
  DELETE /api/chat/sessions/{id}       — delete a session
"""

import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db
from backend.models import ChatSession, ChatMessage, Repository
from backend.services.rag import chat, stream_chat, load_history

router  = APIRouter()
logger  = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question:   str
    session_id: int | None = None


class MessageResponse(BaseModel):
    id:         int
    role:       str
    content:    str
    created_at: str

    class Config:
        from_attributes = True


class SessionResponse(BaseModel):
    id:           int
    repo_id:      int
    session_name: str

    class Config:
        from_attributes = True


# ── Non-streaming chat ────────────────────────────────────────────────────────

@router.post("/{repo_id}/message")
def send_message(
    repo_id: int,
    payload: ChatRequest,
    db: Session = Depends(get_db),
):
    """
    Non-streaming chat turn. Returns the full answer at once.
    Use this for simple integrations or testing.
    """
    try:
        result = chat(
            db=db,
            repo_id=repo_id,
            question=payload.question,
            session_id=payload.session_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Chat error for repo {repo_id}: {e}")
        raise HTTPException(status_code=500, detail="LLM call failed. Check your API key.")


# ── Streaming chat ────────────────────────────────────────────────────────────

@router.post("/{repo_id}/stream")
def stream_message(
    repo_id: int,
    payload: ChatRequest,
    db: Session = Depends(get_db),
):
    """
    Streaming chat using Server-Sent Events (SSE).

    The client receives tokens as they arrive from Gemini.
    Final event contains sources metadata as JSON.

    Event format:
        data: Hello
        data:  there
        data: {"__sources__": [...]}  ← final event with citations
    """
    def event_generator():
        try:
            for token_or_meta in stream_chat(
                db=db,
                repo_id=repo_id,
                question=payload.question,
                session_id=payload.session_id,
            ):
                if isinstance(token_or_meta, dict):
                    # Sources metadata — send as JSON event
                    yield f"data: {json.dumps(token_or_meta)}\n\n"
                else:
                    # Text token — escape newlines for SSE format
                    safe = token_or_meta.replace("\n", "\\n")
                    yield f"data: {safe}\n\n"

        except ValueError as e:
            yield f"data: {json.dumps({'__error__': str(e)})}\n\n"
        except Exception as e:
            logger.exception(f"Streaming error: {e}")
            yield f"data: {json.dumps({'__error__': 'Stream failed'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disables Nginx buffering
        },
    )


# ── Session management ────────────────────────────────────────────────────────

@router.get("/{repo_id}/sessions", response_model=list[SessionResponse])
def list_sessions(repo_id: int, db: Session = Depends(get_db)):
    """List all chat sessions for a repository, newest first."""
    return (
        db.query(ChatSession)
        .filter(ChatSession.repo_id == repo_id)
        .order_by(ChatSession.created_at.desc())
        .all()
    )


@router.get("/sessions/{session_id}/history")
def get_history(session_id: int, db: Session = Depends(get_db)):
    """Return full message history for a session."""
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "session_id":   session_id,
        "session_name": session.session_name,
        "messages": [
            {
                "id":      m.id,
                "role":    m.role,
                "content": m.content,
            }
            for m in messages
        ],
    }


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, db: Session = Depends(get_db)):
    """Delete a chat session and all its messages."""
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()