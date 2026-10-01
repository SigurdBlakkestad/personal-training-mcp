from pathlib import Path

import pytest

from training_pipeline.shared.config import DEFAULT_TIMEZONE, Settings


def test_empty_string_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset GitHub Actions `vars.*`/`secrets.*` still sets the env var to "".

    Without `env_ignore_empty`, pydantic-settings raises a ValidationError
    parsing "" as an int instead of falling back to the field's default.
    """
    monkeypatch.setenv("ATHLETE_HR_REST", "")
    monkeypatch.setenv("ATHLETE_HR_MAX", "")

    settings = Settings()  # type: ignore[call-arg]

    assert settings.ATHLETE_HR_REST == 49
    assert settings.ATHLETE_HR_MAX == 193


def test_tests_ignore_developer_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/conftest.py keeps a developer's `.env` out of the suite."""
    (tmp_path / ".env").write_text("ATHLETE_TZ=America/New_York\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings()  # type: ignore[call-arg]

    assert settings.ATHLETE_TZ == DEFAULT_TIMEZONE
