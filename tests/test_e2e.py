"""E2E tests against a real PostgreSQL instance via testcontainers.

Run with: uv run pytest -m e2e
Requires: Docker daemon running
"""

from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from db_migrate import (
    MigrationError,
    get_applied_versions,
    run_migrate,
    run_rollback,
    run_status,
)

pytestmark = pytest.mark.e2e

SCHEMA = "public"


@pytest.fixture(scope="module")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine", driver=None) as pg:
        yield pg


@pytest.fixture
async def conn(postgres_container: PostgresContainer) -> AsyncGenerator[asyncpg.Connection, None]:
    url = postgres_container.get_connection_url()
    c = await asyncpg.connect(url)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "20260101000000_create_users.sql").write_text(
        "-- migrate:up\n"
        "CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT NOT NULL);\n\n"
        "-- migrate:down\n"
        "DROP TABLE users;\n"
    )
    (d / "20260102000000_add_email.sql").write_text(
        "-- migrate:up\n"
        "ALTER TABLE users ADD COLUMN email TEXT;\n\n"
        "-- migrate:down\n"
        "ALTER TABLE users DROP COLUMN email;\n"
    )
    return d


class TestMigrateE2E:
    """Sequential lifecycle: apply -> idempotent -> rollback -> rollback -> empty -> reapply.

    Tests depend on execution order — each test builds on the DB state left by the previous one.
    """

    async def test_apply_all_migrations(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        count = await run_migrate(conn, SCHEMA, migrations_dir)
        assert count == 2

        applied = await get_applied_versions(conn, SCHEMA)
        assert applied == {"20260101000000", "20260102000000"}

        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'users' ORDER BY ordinal_position"
        )
        col_names = [r["column_name"] for r in cols]
        assert "id" in col_names
        assert "name" in col_names
        assert "email" in col_names

    async def test_idempotent_migrate(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        count = await run_migrate(conn, SCHEMA, migrations_dir)
        assert count == 0

    async def test_rollback_latest(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        result = await run_rollback(conn, SCHEMA, migrations_dir)
        assert result is True

        applied = await get_applied_versions(conn, SCHEMA)
        assert applied == {"20260101000000"}

        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
        )
        col_names = [r["column_name"] for r in cols]
        assert "email" not in col_names

    async def test_rollback_again(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        result = await run_rollback(conn, SCHEMA, migrations_dir)
        assert result is True

        applied = await get_applied_versions(conn, SCHEMA)
        assert applied == set()

        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'users'"
        )
        assert len(tables) == 0

    async def test_rollback_empty_is_noop(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        result = await run_rollback(conn, SCHEMA, migrations_dir)
        assert result is False

    async def test_reapply_after_full_rollback(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        count = await run_migrate(conn, SCHEMA, migrations_dir)
        assert count == 2


class TestStatusE2E:
    async def test_status_output(
        self,
        conn: asyncpg.Connection,
        migrations_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await run_migrate(conn, SCHEMA, migrations_dir)
        await run_status(conn, SCHEMA, migrations_dir)

        output = capsys.readouterr().out
        assert "applied" in output
        assert "All migrations applied" in output


class TestCustomSchemaE2E:
    @pytest.fixture
    def schema_migrations_dir(self, tmp_path: Path) -> Path:
        """Separate migrations using schema-qualified tables to avoid conflicts."""
        d = tmp_path / "schema_migrations"
        d.mkdir()
        (d / "20260101000000_create_items.sql").write_text(
            "-- migrate:up\n"
            "CREATE TABLE myapp.items (id SERIAL PRIMARY KEY, label TEXT NOT NULL);\n\n"
            "-- migrate:down\n"
            "DROP TABLE myapp.items;\n"
        )
        return d

    async def test_migrate_with_custom_schema(
        self, conn: asyncpg.Connection, schema_migrations_dir: Path
    ) -> None:
        schema = "myapp"
        count = await run_migrate(conn, schema, schema_migrations_dir)
        assert count == 1

        applied = await get_applied_versions(conn, schema)
        assert len(applied) == 1

        row = await conn.fetchrow("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'myapp'")
        assert row is not None

        result = await run_rollback(conn, schema, schema_migrations_dir)
        assert result is True

    async def test_invalid_schema_rejected(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        with pytest.raises(MigrationError, match="invalid schema name"):
            await run_migrate(conn, "bad;schema", migrations_dir)
