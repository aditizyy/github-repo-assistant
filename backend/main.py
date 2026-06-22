"""
main.py — FastAPI application entry point
Registers routers, CORS, and startup/shutdown events.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.database import engine, Base
from backend.routers import repo, chat, docs, analysis
from backend.config import settings

# Create all tables (safe to call multiple times; won't drop existing)
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="GitHub Repo Assistant API",
    description="AI-powered codebase analysis and chat",
    version="1.0.0",
    docs_url="/api/swagger",   # moved off /api/docs — that prefix now belongs
    redoc_url="/api/redoc",    # to the documentation-generation router below
)

# CORS — allow Streamlit (port 8501) and future React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",   # Streamlit
        "http://localhost:3000",   # React (future)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers with URL prefixes
app.include_router(repo.router,      prefix="/api/repos",    tags=["Repositories"])
app.include_router(chat.router,      prefix="/api/chat",     tags=["Chat"])
app.include_router(docs.router,      prefix="/api/docs",     tags=["Documentation"])
app.include_router(analysis.router,  prefix="/api/analysis", tags=["Analysis"])


@app.get("/health")
def health_check():
    """Quick check that the server is alive."""
    return {"status": "ok", "env": settings.app_env}