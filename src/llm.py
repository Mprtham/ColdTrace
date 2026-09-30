"""Chat model factory. Provider and model come from .env (docs/DESIGN.md §5.1)."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from src.config import get_settings

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def get_llm() -> BaseChatModel:
    settings = get_settings()
    if settings.llm_provider == "deepseek":
        if not (settings.deepseek_api_key and settings.deepseek_model):
            raise RuntimeError("LLM_PROVIDER=deepseek needs DEEPSEEK_API_KEY and DEEPSEEK_MODEL")
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        return ChatOpenAI(
            model=settings.deepseek_model,
            api_key=SecretStr(settings.deepseek_api_key),
            base_url=DEEPSEEK_BASE_URL,
            temperature=0,
        )
    from langchain_ollama import ChatOllama

    return ChatOllama(model=settings.ollama_model, base_url=settings.ollama_base_url, temperature=0)
