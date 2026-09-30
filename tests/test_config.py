import pytest

from src.config import Settings


def test_defaults_use_local_ollama_and_small_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "ollama"
    assert settings.embedding_dim == 384


def test_env_overrides_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_MODEL", "some-model-id")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "deepseek"
    assert settings.deepseek_model == "some-model-id"
