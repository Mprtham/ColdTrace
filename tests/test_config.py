import pytest

from src.config import Settings


def test_defaults_use_local_ollama_and_small_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "ollama"
    assert settings.embedding_dim == 384


@pytest.mark.parametrize(
    "given",
    [
        "postgres://u:p@ep-x.neon.tech/coldtrace?sslmode=require",
        "postgresql://u:p@ep-x.neon.tech/coldtrace?sslmode=require",
        "postgresql+psycopg://u:p@ep-x.neon.tech/coldtrace?sslmode=require",
    ],
)
def test_database_url_is_pinned_to_psycopg3(monkeypatch: pytest.MonkeyPatch, given: str) -> None:
    monkeypatch.setenv("DATABASE_URL", given)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.require_database_url() == (
        "postgresql+psycopg://u:p@ep-x.neon.tech/coldtrace?sslmode=require"
    )


def test_missing_database_url_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        settings.require_database_url()


def test_env_overrides_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_MODEL", "some-model-id")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "deepseek"
    assert settings.deepseek_model == "some-model-id"


def test_psycopg_url_strips_sqlalchemy_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@ep-x.neon.tech/db?sslmode=require")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.psycopg_url() == "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"


def test_bad_connection_error_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import psycopg

    from src.db import scrubbed_errors

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2secret@ep-x.neon.tech/db")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(SystemExit), scrubbed_errors(settings.secrets()):
        # The SQLAlchemy-form URL makes libpq echo the full string in its error.
        psycopg.connect(settings.require_database_url())
    err = capsys.readouterr().err
    assert "hunter2secret" not in err
    assert "***" in err
