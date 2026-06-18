"""
config.py — Centralised settings loaded from .env
Why: Single source of truth. Never scatter env reads across files.
"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Database
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "repo_assistant"
    db_user: str = "root"
    db_password: str = ""

    # AI
    gemini_api_key: str = ""

    # Paths
    repos_dir: Path = Path("./cloned_repos")
    faiss_dir: Path = Path("./faiss_indexes")

    # App
    app_env: str = "development"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def db_url(self) -> str:
        return (
            f"mysql+mysqlconnector://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


# Singleton — import this everywhere
settings = Settings()

# Create required directories on startup
settings.repos_dir.mkdir(parents=True, exist_ok=True)
settings.faiss_dir.mkdir(parents=True, exist_ok=True)