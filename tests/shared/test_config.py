import pytest

from training_pipeline.shared.config import Settings


def test_empty_string_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset GitHub Actions `vars.*`/`secrets.*` still sets the env var to "".

    Without `env_ignore_empty`, pydantic-settings raises a ValidationError
    parsing "" as an int instead of falling back to the field's default.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("ATHLETE_HR_REST", "")
    monkeypatch.setenv("ATHLETE_HR_MAX", "")

    settings = Settings()  # type: ignore[call-arg]

    assert settings.ATHLETE_HR_REST == 49
    assert settings.ATHLETE_HR_MAX == 193
