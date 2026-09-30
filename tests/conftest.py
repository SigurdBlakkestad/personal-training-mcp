import pytest

from training_pipeline.shared.config import Settings


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build Settings from defaults, never from a developer's `.env`.

    Without this, any code path that calls `get_settings()` fails on a clean
    checkout (DATABASE_URL is required), and on a machine with a `.env` the
    tests silently pick up real values such as ATHLETE_TZ. Unit tests never
    touch a real database, so a dummy URL is enough.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
