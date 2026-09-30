"""Runtime configuration, read from environment variables or `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL (Neon free tier in development; Render in deploy)
    database_url: str = "postgresql+psycopg://coldtrace:coldtrace@localhost:5432/coldtrace"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
