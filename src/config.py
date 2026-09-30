"""Runtime configuration, read from environment variables or `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# Anchored to the repo root, so scripts find .env regardless of the working directory.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # PostgreSQL (Neon free tier in development; Render in deploy). No default:
    # a missing DATABASE_URL must fail loudly, not silently fall back to localhost.
    database_url: str | None = None
    coldtrace_admin_password: str | None = None  # scripts/setup_db.py, ingest
    coldtrace_agent_password: str | None = None  # the app

    # live: as_of = now. replay: as_of = newest reading in the database, so a demo of the
    # static synthetic fleet does not go STALE_FEED 15 minutes after ingest.
    clock_mode: Literal["live", "replay"] = "live"

    # UI -> API. The Streamlit app only talks HTTP; it never touches the database.
    api_url: str = "http://localhost:8000"

    # USD per million tokens, for the cost-per-query footer. Unset = cost not shown
    # (a local Ollama model costs nothing per token; DeepSeek prices change, so they
    # are configuration, not code).
    llm_price_in_per_mtok: float | None = None
    llm_price_out_per_mtok: float | None = None

    # Qdrant: local on-disk mode unless a server URL is given
    qdrant_path: str = "./qdrant_storage"
    qdrant_url: str | None = None

    # Embeddings (docs/DESIGN.md §4.3 — locked to 384-dim)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # LLM: Ollama locally until a DeepSeek key is available
    llm_provider: Literal["ollama", "deepseek"] = "ollama"
    ollama_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"
    deepseek_api_key: str | None = None
    deepseek_model: str | None = None  # set from GET api.deepseek.com/v1/models

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3_driver(cls, url: str | None) -> str | None:
        # Neon and Render hand out postgres:// or postgresql:// URLs, which SQLAlchemy
        # maps to psycopg2. We install psycopg (v3), so pin the driver explicitly.
        if url is None:
            return None
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url.removeprefix(prefix)
        return url

    def require_database_url(self) -> str:
        """SQLAlchemy form (postgresql+psycopg://) — for create_engine() only."""
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
        return self.database_url

    def psycopg_url(self) -> str:
        """Plain libpq form (postgresql://) — for psycopg.connect(). Passing the SQLAlchemy
        form to psycopg fails, and libpq echoes the whole URL, password included."""
        return self.require_database_url().replace("postgresql+psycopg://", "postgresql://", 1)

    def secrets(self) -> list[str]:
        """Every password this app knows, for scrubbing error output."""
        found = [self.coldtrace_admin_password, self.coldtrace_agent_password]
        if self.database_url:
            found.append(make_url(self.database_url).password)
        found.append(self.deepseek_api_key)
        return [s for s in found if s]


@lru_cache
def get_settings() -> Settings:
    return Settings()
