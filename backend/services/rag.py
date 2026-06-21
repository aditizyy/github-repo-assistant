"""
rag.py — Business logic layer between the router and the RAG chain.

Responsibilities:
  - Load/create chat sessions
  - Persist messages to MySQL
  - Call the RAG chain
  - Return structured responses

The router stays thin. Business logic lives here.
"""

import logging
from sqlalchemy.orm import Session

from backend.models import Repository, ChatSession, ChatMessage
from ai.chains import RAGChain

logger = logging.getLogger(__name__)

# One RAGChain instance shared across requests
_rag_chain = RAGChain()


def get_or_create_session(
    db: Session,
    repo_id: int,
    session_id: int | None = None,
) -> ChatSession:
    """
    Returns an existing session or creates a new one.
    If session_id is None, always creates a new session.
    """
    if session_id:
        session = db.query(ChatSession).filter(
            ChatSession.id == session_id,
            ChatSession.repo_id == repo_id,
        ).first()
        if session:
            return session

    session = ChatSession(repo_id=repo_id, session_name="New Chat")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def load_history(db: Session, session_id: int) -> list[dict]:
    """
    Load conversation history for a session as a list of dicts.
    Returns last 20 messages (10 turns) — enough for context continuity.
    """
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(20)
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in messages]


def save_message(
    db: Session,
    session_id: int,
    role: str,
    content: str,
) -> ChatMessage:
    """Persist one message turn to MySQL."""
    msg = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def chat(
    db: Session,
    repo_id: int,
    question: str,
    session_id: int | None = None,
) -> dict:
    """
    Full non-streaming chat turn:
      1. Load/create session
      2. Load history
      3. Save user message
      4. Run RAG chain
      5. Save assistant message
      6. Return response

    Returns:
        {
            "session_id": int,
            "answer":     str,
            "sources":    list,
            "rewritten_query": str,
        }
    """
    # Validate repository exists and is ready
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise ValueError(f"Repository {repo_id} not found")
    if repo.status != "ready":
        raise ValueError(f"Repository not ready (status: {repo.status})")

    session = get_or_create_session(db, repo_id, session_id)
    history = load_history(db, session.id)

    # Persist user message before calling LLM
    # (so it's saved even if LLM call fails)
    save_message(db, session.id, "user", question)

    # Run RAG chain
    result = _rag_chain.invoke(
        repo_id=repo_id,
        question=question,
        history=history,
    )

    # Persist assistant response
    save_message(db, session.id, "assistant", result["answer"])

    # Auto-name session after first question
    if len(history) == 0:
        session.session_name = question[:60] + ("..." if len(question) > 60 else "")
        db.commit()

    return {
        "session_id":      session.id,
        "answer":          result["answer"],
        "sources":         result["sources"],
        "rewritten_query": result["rewritten_query"],
    }


def stream_chat(
    db: Session,
    repo_id: int,
    question: str,
    session_id: int | None = None,
):
    """
    Streaming chat — yields tokens then saves to DB when complete.

    This is a generator. The router wraps it in a StreamingResponse.

    Yields:
        str tokens from LLM, then final metadata dict
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise ValueError(f"Repository {repo_id} not found")
    if repo.status != "ready":
        raise ValueError(f"Repository not ready (status: {repo.status})")

    session = get_or_create_session(db, repo_id, session_id)
    history = load_history(db, session.id)

    save_message(db, session.id, "user", question)

    final_metadata = None

    for token_or_meta in _rag_chain.stream(repo_id, question, history):
        if isinstance(token_or_meta, dict) and "__sources__" in token_or_meta:
            # Final metadata payload from the chain
            final_metadata = token_or_meta
        else:
            yield token_or_meta

    # Save full response to DB after streaming completes
    if final_metadata:
        full_response = final_metadata.get("__full_response__", "")
        save_message(db, session.id, "assistant", full_response)

        if len(history) == 0:
            session.session_name = question[:60]
            db.commit()

        # Yield sources as a final JSON payload
        yield final_metadata