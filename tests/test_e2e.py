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
    ADVISORY_LOCK_ID,
    MigrationError,
    acquire_lock,
    get_applied_versions,
    release_lock,
    run_baseline,
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
        assert result == 1

        applied = await get_applied_versions(conn, SCHEMA)
        assert applied == {"20260101000000"}

        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
        )
        col_names = [r["column_name"] for r in cols]
        assert "email" not in col_names

    async def test_rollback_again(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        result = await run_rollback(conn, SCHEMA, migrations_dir)
        assert result == 1

        applied = await get_applied_versions(conn, SCHEMA)
        assert applied == set()

        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'users'"
        )
        assert len(tables) == 0

    async def test_rollback_empty_is_noop(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        result = await run_rollback(conn, SCHEMA, migrations_dir)
        assert result == 0

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
        assert result == 1

    async def test_invalid_schema_rejected(self, conn: asyncpg.Connection, migrations_dir: Path) -> None:
        with pytest.raises(MigrationError, match="invalid schema name"):
            await run_migrate(conn, "bad;schema", migrations_dir)


class TestDryRunE2E:
    async def test_dry_run_reports_without_applying(self, conn: asyncpg.Connection, tmp_path: Path) -> None:
        d = tmp_path / "dryrun_migrations"
        d.mkdir()
        # Version chosen to not collide with other suites sharing the module-scoped container
        (d / "20990201000000_create_dryrun.sql").write_text(
            "-- migrate:up\nCREATE TABLE dryrun_t (id INT);\n\n-- migrate:down\nDROP TABLE dryrun_t;\n"
        )

        count = await run_migrate(conn, SCHEMA, d, dry_run=True)
        assert count == 1

        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'dryrun_t'"
        )
        assert len(tables) == 0

        applied = await get_applied_versions(conn, SCHEMA)
        assert "20990201000000" not in applied


class TestBaselineE2E:
    async def test_baseline_marks_without_executing(self, conn: asyncpg.Connection, tmp_path: Path) -> None:
        d = tmp_path / "baseline_migrations"
        d.mkdir()
        (d / "20990101000000_create_baseline.sql").write_text(
            "-- migrate:up\nCREATE TABLE baseline_t (id INT);\n\n-- migrate:down\nDROP TABLE baseline_t;\n"
        )

        count = await run_baseline(conn, SCHEMA, d)
        assert count == 1

        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'baseline_t'"
        )
        assert len(tables) == 0

        applied = await get_applied_versions(conn, SCHEMA)
        assert "20990101000000" in applied

        # Baselined version is no longer pending
        count = await run_migrate(conn, SCHEMA, d)
        assert count == 0


class TestNoTransactionE2E:
    async def test_create_index_concurrently(self, conn: asyncpg.Connection, tmp_path: Path) -> None:
        d = tmp_path / "notx_migrations"
        d.mkdir()
        (d / "20990301000000_create_notx.sql").write_text(
            "-- migrate:up\nCREATE TABLE notx_t (id INT);\n\n-- migrate:down\nDROP TABLE notx_t;\n"
        )
        # CREATE INDEX CONCURRENTLY cannot run inside a transaction block —
        # this fails unless transaction:false is honored.
        (d / "20990302000000_add_index.sql").write_text(
            "-- migrate:up transaction:false\n"
            "CREATE INDEX CONCURRENTLY notx_idx ON notx_t (id);\n\n"
            "-- migrate:down transaction:false\n"
            "DROP INDEX CONCURRENTLY notx_idx;\n"
        )

        count = await run_migrate(conn, SCHEMA, d)
        assert count == 2

        row = await conn.fetchrow("SELECT 1 FROM pg_indexes WHERE indexname = 'notx_idx'")
        assert row is not None

        result = await run_rollback(conn, SCHEMA, d, count=2)
        assert result == 2

        row = await conn.fetchrow("SELECT 1 FROM pg_indexes WHERE indexname = 'notx_idx'")
        assert row is None

    async def test_multi_statement_block_raises_clear_error(
        self, conn: asyncpg.Connection, tmp_path: Path
    ) -> None:
        """asyncpg runs multi-statement strings in an implicit transaction — the tool must
        surface that as a MigrationError with a hint, not a raw driver exception."""
        d = tmp_path / "multistmt_migrations"
        d.mkdir()
        (d / "20990401000000_t.sql").write_text(
            "-- migrate:up\nCREATE TABLE msf_t (id INT);\n\n-- migrate:down\nDROP TABLE msf_t;\n"
        )
        (d / "20990402000000_bad.sql").write_text(
            "-- migrate:up transaction:false\n"
            "SET statement_timeout = 0;\n"
            "CREATE INDEX CONCURRENTLY msf_idx ON msf_t (id);\n\n"
            "-- migrate:down\nDROP INDEX msf_idx;\n"
        )

        with pytest.raises(MigrationError, match="single statement"):
            await run_migrate(conn, SCHEMA, d)

        # First migration applied, failing one left pending — consistent state
        applied = await get_applied_versions(conn, SCHEMA)
        assert "20990401000000" in applied
        assert "20990402000000" not in applied

        # Targeted cleanup — order-independent, unlike run_rollback which pops the
        # global max version of the shared container
        await conn.execute("DROP TABLE msf_t")
        await conn.execute("DELETE FROM schema_migrations WHERE version = $1", "20990401000000")


class TestAdvisoryLockE2E:
    async def test_lock_blocks_second_session(
        self, postgres_container: PostgresContainer, conn: asyncpg.Connection
    ) -> None:
        await acquire_lock(conn)
        try:
            other = await asyncpg.connect(postgres_container.get_connection_url())
            try:
                got_it = await other.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID)
                assert got_it is False
            finally:
                await other.close()
        finally:
            await release_lock(conn)
