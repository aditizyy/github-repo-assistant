"""
models.py — SQLAlchemy ORM models mirroring schema.sql
These give you Python objects instead of raw SQL dictionaries.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Enum, ForeignKey,
    TIMESTAMP, func
)
from sqlalchemy.orm import relationship
from backend.database import Base


class Repository(Base):
    __tablename__ = "repositories"

    id            = Column(Integer, primary_key=True, index=True)
    github_url    = Column(String(500), nullable=False)
    repo_name     = Column(String(255), nullable=False)
    clone_path    = Column(String(500))
    status        = Column(
        Enum("pending", "cloning", "indexing", "ready", "error"),
        default="pending"
    )
    file_count    = Column(Integer, default=0)
    chunk_count   = Column(Integer, default=0)
    error_message = Column(Text)
    created_at    = Column(TIMESTAMP, server_default=func.now())
    updated_at    = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    sessions      = relationship("ChatSession", back_populates="repository", cascade="all, delete")
    files         = relationship("IndexedFile", back_populates="repository", cascade="all, delete")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id           = Column(Integer, primary_key=True, index=True)
    repo_id      = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    session_name = Column(String(255), default="New Chat")
    created_at   = Column(TIMESTAMP, server_default=func.now())

    repository   = relationship("Repository", back_populates="sessions")
    messages     = relationship("ChatMessage", back_populates="session", cascade="all, delete")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id          = Column(Integer, primary_key=True, index=True)
    session_id  = Column(Integer, ForeignKey("chat_sessions.id"), nullable=False)
    role        = Column(Enum("user", "assistant"), nullable=False)
    content     = Column(Text, nullable=False)
    tokens_used = Column(Integer, default=0)
    created_at  = Column(TIMESTAMP, server_default=func.now())

    session     = relationship("ChatSession", back_populates="messages")


class IndexedFile(Base):
    __tablename__ = "indexed_files"

    id          = Column(Integer, primary_key=True, index=True)
    repo_id     = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    file_path   = Column(String(1000), nullable=False)
    language    = Column(String(50))
    line_count  = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    created_at  = Column(TIMESTAMP, server_default=func.now())

    repository  = relationship("Repository", back_populates="files")