from pathlib import Path

import pytest


@pytest.fixture
def tmp_migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d
