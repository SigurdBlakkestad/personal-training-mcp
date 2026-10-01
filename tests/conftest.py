import os

import pytest

from training_pipeline.shared.config import Settings


def pytest_configure(config: pytest.Config) -> None:
    """Build Settings from defaults, never from a developer's `.env` or shell.

    Without this, any code path that calls `get_settings()` fails on a clean
    checkout (DATABASE_URL is required), and on a machine with a `.env` (or
    one exported by direnv / an editor) the tests silently pick up real values
    such as ATHLETE_TZ. Done here rather than in a fixture so it also covers
    modules that read settings at import time, during collection. Unit tests
    never touch a real database, so a dummy URL is enough.
    """
    Settings.model_config["env_file"] = None
    for name in Settings.model_fields:
        os.environ.pop(name, None)
    os.environ["DATABASE_URL"] = "postgresql://localhost/test"
