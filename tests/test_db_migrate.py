import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import db_migrate
from db_migrate import (
    ADVISORY_LOCK_ID,
    MigrationError,
    __version__,
    async_main,
    build_parser,
    create_migration,
    discover_migrations,
    ensure_schema_and_table,
    get_applied_versions,
    normalize_database_url,
    parse_migration,
    parse_transaction_option,
    run_baseline,
    run_migrate,
    run_rollback,
    run_status,
    validate_schema_name,
)

# --- Pure function tests ---


class TestVersion:
    def test_matches_pyproject(self) -> None:
        """__version__ in db_migrate.py is the source of truth; pyproject.toml must mirror it."""
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        assert data["project"]["version"] == __version__


class TestValidateSchemaName:
    def test_accepts_valid_identifiers(self) -> None:
        for name in ("public", "my_schema", "_private", "Schema1"):
            validate_schema_name(name)

    def test_rejects_invalid_identifiers(self) -> None:
        for name in ("", "1start", "has space", "semi;colon", "my-schema", "a.b", 'a"b'):
            with pytest.raises(MigrationError):
                validate_schema_name(name)


class TestParseMigration:
    def test_extracts_up_block(self) -> None:
        content = "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        assert parse_migration(content, "up") == "CREATE TABLE t (id INT);"

    def test_extracts_down_block(self) -> None:
        content = "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        assert parse_migration(content, "down") == "DROP TABLE t;"

    def test_returns_none_for_missing_block(self) -> None:
        content = "-- migrate:up\nCREATE TABLE t (id INT);\n"
        assert parse_migration(content, "down") is None

    def test_returns_none_for_empty_block(self) -> None:
        content = "-- migrate:up\n\n-- migrate:down\n"
        assert parse_migration(content, "up") is None

    def test_tolerates_marker_options(self) -> None:
        content = (
            "-- migrate:up transaction:false\nCREATE INDEX CONCURRENTLY i;\n\n"
            "-- migrate:down\nDROP INDEX i;\n"
        )
        assert parse_migration(content, "up") == "CREATE INDEX CONCURRENTLY i;"
        assert parse_migration(content, "down") == "DROP INDEX i;"

    def test_handles_multiline_sql(self) -> None:
        content = (
            "-- migrate:up\nCREATE TABLE t (\n  id INT,\n  name TEXT\n);\n\n-- migrate:down\nDROP TABLE t;\n"
        )
        result = parse_migration(content, "up")
        assert result is not None
        assert "id INT" in result
        assert "name TEXT" in result


class TestParseTransactionOption:
    def test_defaults_to_true(self) -> None:
        content = "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        assert parse_transaction_option(content, "up") is True
        assert parse_transaction_option(content, "down") is True

    def test_transaction_false(self) -> None:
        content = (
            "-- migrate:up transaction:false\nCREATE INDEX CONCURRENTLY i;\n\n"
            "-- migrate:down\nDROP INDEX i;\n"
        )
        assert parse_transaction_option(content, "up") is False
        assert parse_transaction_option(content, "down") is True

    def test_transaction_false_on_down_only(self) -> None:
        content = (
            "-- migrate:up\nCREATE INDEX i;\n\n"
            "-- migrate:down transaction:false\nDROP INDEX CONCURRENTLY i;\n"
        )
        assert parse_transaction_option(content, "up") is True
        assert parse_transaction_option(content, "down") is False

    def test_case_insensitive(self) -> None:
        content = "-- migrate:up TRANSACTION:FALSE\nSELECT 1;\n"
        assert parse_transaction_option(content, "up") is False

    def test_explicit_transaction_true(self) -> None:
        content = "-- migrate:up transaction:true\nSELECT 1;\n"
        assert parse_transaction_option(content, "up") is True

    def test_missing_block_defaults_true(self) -> None:
        assert parse_transaction_option("-- migrate:up\nSELECT 1;\n", "down") is True


class TestNormalizeDatabaseUrl:
    def test_strips_asyncpg_driver(self) -> None:
        url = "postgresql+asyncpg://user:pass@localhost/db"
        assert normalize_database_url(url) == "postgresql://user:pass@localhost/db"

    def test_leaves_plain_url_unchanged(self) -> None:
        url = "postgresql://user:pass@localhost/db"
        assert normalize_database_url(url) == url


class TestDiscoverMigrations:
    def test_returns_sorted_migrations(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260102000000_second.sql").write_text("-- migrate:up\n")
        (tmp_migrations_dir / "20260101000000_first.sql").write_text("-- migrate:up\n")
        result = discover_migrations(tmp_migrations_dir)
        assert len(result) == 2
        assert result[0][0] == "20260101000000"
        assert result[1][0] == "20260102000000"

    def test_skips_invalid_filenames(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_valid.sql").write_text("-- migrate:up\n")
        (tmp_migrations_dir / "bad_name.sql").write_text("-- migrate:up\n")
        result = discover_migrations(tmp_migrations_dir)
        assert len(result) == 1

    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        assert discover_migrations(tmp_path / "nonexistent") == []


class TestCreateMigration:
    def test_creates_file_with_template(self, tmp_migrations_dir: Path) -> None:
        path = create_migration(tmp_migrations_dir, "add users table")
        assert path.exists()
        content = path.read_text()
        assert "-- migrate:up" in content
        assert "-- migrate:down" in content

    def test_filename_has_timestamp_and_slug(self, tmp_migrations_dir: Path) -> None:
        path = create_migration(tmp_migrations_dir, "Add Users Table!")
        assert path.name.endswith("_add_users_table.sql")
        assert len(path.stem.split("_")[0]) == 14

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        d = tmp_path / "new" / "migrations"
        path = create_migration(d, "init")
        assert path.exists()
        assert d.exists()

    def test_all_punctuation_description_gets_fallback_slug(self, tmp_migrations_dir: Path) -> None:
        path = create_migration(tmp_migrations_dir, "!!! ???")
        assert path.name.endswith("_migration.sql")


# --- Async function tests (mocked connection) ---


def _mock_conn() -> AsyncMock:
    """Create a mock asyncpg connection with transaction context manager."""
    conn = AsyncMock()
    # asyncpg's transaction() is a regular method returning an async context manager
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


class TestEnsureSchemaAndTable:
    @pytest.mark.asyncio
    async def test_creates_schema_when_not_public(self) -> None:
        conn = _mock_conn()
        await ensure_schema_and_table(conn, "myapp")
        calls = [c.args[0] for c in conn.execute.call_args_list]
        assert any("CREATE SCHEMA IF NOT EXISTS myapp" in c for c in calls)
        assert any("schema_migrations" in c for c in calls)

    @pytest.mark.asyncio
    async def test_skips_schema_creation_for_public(self) -> None:
        conn = _mock_conn()
        await ensure_schema_and_table(conn, "public")
        calls = [c.args[0] for c in conn.execute.call_args_list]
        assert not any("CREATE SCHEMA" in c for c in calls)
        assert any("schema_migrations" in c for c in calls)

    @pytest.mark.asyncio
    async def test_rejects_invalid_schema(self) -> None:
        conn = _mock_conn()
        with pytest.raises(MigrationError, match="invalid schema name"):
            await ensure_schema_and_table(conn, "bad;schema")


class TestGetAppliedVersions:
    @pytest.mark.asyncio
    async def test_returns_version_set(self) -> None:
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}, {"version": "20260102000000"}]
        result = await get_applied_versions(conn, "public")
        assert result == {"20260101000000", "20260102000000"}

    @pytest.mark.asyncio
    async def test_returns_empty_set(self) -> None:
        conn = _mock_conn()
        conn.fetch.return_value = []
        result = await get_applied_versions(conn, "public")
        assert result == set()


class TestRunMigrate:
    @pytest.mark.asyncio
    async def test_applies_pending_migrations(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text(
            "-- migrate:up\nCREATE TABLE t1 (id INT);\n\n-- migrate:down\nDROP TABLE t1;\n"
        )
        (tmp_migrations_dir / "20260102000000_second.sql").write_text(
            "-- migrate:up\nCREATE TABLE t2 (id INT);\n\n-- migrate:down\nDROP TABLE t2;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        count = await run_migrate(conn, "public", tmp_migrations_dir)

        assert count == 1
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert any("CREATE TABLE t2" in c for c in execute_calls)
        assert not any("CREATE TABLE t1" in c for c in execute_calls)

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_pending(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text(
            "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        count = await run_migrate(conn, "public", tmp_migrations_dir)
        assert count == 0

    @pytest.mark.asyncio
    async def test_raises_on_missing_up_block(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_bad.sql").write_text("-- migrate:down\nDROP TABLE t;\n")
        conn = _mock_conn()
        conn.fetch.return_value = []

        with pytest.raises(MigrationError, match="migrate:up"):
            await run_migrate(conn, "public", tmp_migrations_dir)

    @pytest.mark.asyncio
    async def test_dry_run_executes_nothing(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text(
            "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = []

        count = await run_migrate(conn, "public", tmp_migrations_dir, dry_run=True)

        assert count == 1
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert not any("CREATE TABLE t" in c for c in execute_calls)
        assert not any("INSERT INTO" in c for c in execute_calls)


class TestRunMigrateNoTransaction:
    @pytest.mark.asyncio
    async def test_transaction_false_skips_wrapper(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_idx.sql").write_text(
            "-- migrate:up transaction:false\nCREATE INDEX CONCURRENTLY i ON t (id);\n\n"
            "-- migrate:down\nDROP INDEX i;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = []

        count = await run_migrate(conn, "public", tmp_migrations_dir)

        assert count == 1
        conn.transaction.assert_not_called()
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert any("CREATE INDEX CONCURRENTLY" in c for c in execute_calls)
        assert any("INSERT INTO" in c for c in execute_calls)

    @pytest.mark.asyncio
    async def test_default_uses_transaction(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_t.sql").write_text(
            "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = []

        await run_migrate(conn, "public", tmp_migrations_dir)

        conn.transaction.assert_called_once()


class TestRunRollback:
    @pytest.mark.asyncio
    async def test_rolls_back_latest(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text(
            "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        result = await run_rollback(conn, "public", tmp_migrations_dir)

        assert result == 1
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert any("DROP TABLE t" in c for c in execute_calls)
        assert any("DELETE FROM" in c for c in execute_calls)

    @pytest.mark.asyncio
    async def test_rolls_back_multiple(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text(
            "-- migrate:up\nCREATE TABLE t1 (id INT);\n\n-- migrate:down\nDROP TABLE t1;\n"
        )
        (tmp_migrations_dir / "20260102000000_second.sql").write_text(
            "-- migrate:up\nCREATE TABLE t2 (id INT);\n\n-- migrate:down\nDROP TABLE t2;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260102000000"}, {"version": "20260101000000"}]

        result = await run_rollback(conn, "public", tmp_migrations_dir, count=2)

        assert result == 2
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert any("DROP TABLE t2" in c for c in execute_calls)
        assert any("DROP TABLE t1" in c for c in execute_calls)

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_applied(self, tmp_migrations_dir: Path) -> None:
        conn = _mock_conn()
        conn.fetch.return_value = []

        result = await run_rollback(conn, "public", tmp_migrations_dir)
        assert result == 0

    @pytest.mark.asyncio
    async def test_rejects_invalid_count(self, tmp_migrations_dir: Path) -> None:
        conn = _mock_conn()
        with pytest.raises(MigrationError, match="rollback count"):
            await run_rollback(conn, "public", tmp_migrations_dir, count=0)

    @pytest.mark.asyncio
    async def test_raises_when_file_missing(self, tmp_migrations_dir: Path) -> None:
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        with pytest.raises(MigrationError, match="not found on disk"):
            await run_rollback(conn, "public", tmp_migrations_dir)

    @pytest.mark.asyncio
    async def test_raises_on_missing_down_block(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_no_down.sql").write_text(
            "-- migrate:up\nCREATE TABLE t (id INT);\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        with pytest.raises(MigrationError, match="migrate:down"):
            await run_rollback(conn, "public", tmp_migrations_dir)


class TestRunStatus:
    @pytest.mark.asyncio
    async def test_prints_status(self, tmp_migrations_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text("-- migrate:up\n")
        (tmp_migrations_dir / "20260102000000_second.sql").write_text("-- migrate:up\n")
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        await run_status(conn, "public", tmp_migrations_dir)

        output = capsys.readouterr().out
        assert "applied" in output
        assert "pending" in output
        assert "20260101000000_first.sql" in output
        assert "20260102000000_second.sql" in output

    @pytest.mark.asyncio
    async def test_handles_empty_dir(
        self, tmp_migrations_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn = _mock_conn()
        conn.fetch.return_value = []

        await run_status(conn, "public", tmp_migrations_dir)

        output = capsys.readouterr().out
        assert "No migration files" in output


class TestRunRollbackNoTransaction:
    @pytest.mark.asyncio
    async def test_transaction_false_on_down_skips_wrapper(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_idx.sql").write_text(
            "-- migrate:up\nCREATE INDEX i ON t (id);\n\n"
            "-- migrate:down transaction:false\nDROP INDEX CONCURRENTLY i;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        result = await run_rollback(conn, "public", tmp_migrations_dir)

        assert result == 1
        conn.transaction.assert_not_called()
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert any("DROP INDEX CONCURRENTLY" in c for c in execute_calls)
        assert any("DELETE FROM" in c for c in execute_calls)


class TestRunBaseline:
    @pytest.mark.asyncio
    async def test_marks_pending_without_executing_sql(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text(
            "-- migrate:up\nCREATE TABLE t (id INT);\n\n-- migrate:down\nDROP TABLE t;\n"
        )
        conn = _mock_conn()
        conn.fetch.return_value = []

        count = await run_baseline(conn, "public", tmp_migrations_dir)

        assert count == 1
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        assert not any("CREATE TABLE t" in c for c in execute_calls)
        insert_calls = [c for c in conn.execute.call_args_list if "INSERT INTO" in str(c.args[0])]
        assert insert_calls[0].args[1] == "20260101000000"

    @pytest.mark.asyncio
    async def test_noop_when_all_applied(self, tmp_migrations_dir: Path) -> None:
        (tmp_migrations_dir / "20260101000000_first.sql").write_text("-- migrate:up\nSELECT 1;\n")
        conn = _mock_conn()
        conn.fetch.return_value = [{"version": "20260101000000"}]

        count = await run_baseline(conn, "public", tmp_migrations_dir)
        assert count == 0


# --- CLI dispatch tests (C1 regression + lock/routing coverage) ---


class TestParserExclusivity:
    def test_conflicting_commands_rejected(self) -> None:
        parser = build_parser()
        combos = [
            ["--dry-run", "--rollback"],
            ["--dry-run", "--rollback", "1"],
            ["--dry-run", "--baseline"],
            ["--status", "--rollback"],
            ["--status", "--baseline"],
            ["--create", "x", "--status"],
            ["--create", "x", "--dry-run"],
            ["--baseline", "--rollback", "2"],
        ]
        for combo in combos:
            with pytest.raises(SystemExit) as exc:
                parser.parse_args(combo)
            assert exc.value.code == 2, f"combo {combo} was not rejected"

    def test_single_commands_accepted(self) -> None:
        parser = build_parser()
        for combo in ([], ["--status"], ["--rollback"], ["--rollback", "3"], ["--baseline"], ["--dry-run"]):
            args = parser.parse_args(combo)
            assert args is not None

    def test_verbose_combines_with_any_command(self) -> None:
        args = build_parser().parse_args(["--dry-run", "--verbose"])
        assert args.dry_run is True
        assert args.verbose is True


class TestAsyncMainDispatch:
    @pytest.fixture
    def dispatch_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_migrations_dir: Path
    ) -> tuple[AsyncMock, dict[str, AsyncMock]]:
        """Patch asyncpg.connect and all run_* entry points; return (conn, run mocks)."""
        conn = _mock_conn()
        monkeypatch.setattr(db_migrate.asyncpg, "connect", AsyncMock(return_value=conn))
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
        monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_migrations_dir))
        monkeypatch.delenv("SCHEMA_NAME", raising=False)
        runs = {}
        for name in ("run_status", "run_rollback", "run_baseline", "run_migrate"):
            mock = AsyncMock(return_value=0)
            monkeypatch.setattr(db_migrate, name, mock)
            runs[name] = mock
        return conn, runs

    def _lock_calls(self, conn: AsyncMock) -> list[str]:
        return [
            str(c.args[0])
            for c in conn.execute.call_args_list
            if "pg_advisory" in str(c.args[0]) and c.args[1:] == (ADVISORY_LOCK_ID,)
        ]

    @pytest.mark.asyncio
    async def test_dry_run_routes_to_migrate_not_rollback(
        self, dispatch_env: tuple[AsyncMock, dict[str, AsyncMock]]
    ) -> None:
        conn, runs = dispatch_env
        await async_main(build_parser().parse_args(["--dry-run"]))
        runs["run_migrate"].assert_awaited_once()
        assert runs["run_migrate"].await_args is not None
        assert runs["run_migrate"].await_args.kwargs["dry_run"] is True
        runs["run_rollback"].assert_not_awaited()
        runs["run_baseline"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rollback_routes_with_count(
        self, dispatch_env: tuple[AsyncMock, dict[str, AsyncMock]]
    ) -> None:
        conn, runs = dispatch_env
        await async_main(build_parser().parse_args(["--rollback", "3"]))
        runs["run_rollback"].assert_awaited_once()
        assert runs["run_rollback"].await_args is not None
        assert runs["run_rollback"].await_args.kwargs["count"] == 3
        runs["run_migrate"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lock_taken_and_released_for_every_command(
        self, dispatch_env: tuple[AsyncMock, dict[str, AsyncMock]]
    ) -> None:
        conn, runs = dispatch_env
        for flags in ([], ["--status"], ["--rollback"], ["--baseline"], ["--dry-run"]):
            conn.execute.reset_mock()
            await async_main(build_parser().parse_args(flags))
            locks = self._lock_calls(conn)
            assert any("pg_advisory_lock" in c for c in locks), f"lock not acquired for {flags}"
            assert any("pg_advisory_unlock" in c for c in locks), f"lock not released for {flags}"

    @pytest.mark.asyncio
    async def test_connection_closed_even_when_command_fails(
        self, dispatch_env: tuple[AsyncMock, dict[str, AsyncMock]]
    ) -> None:
        conn, runs = dispatch_env
        runs["run_migrate"].side_effect = MigrationError("boom")
        with pytest.raises(SystemExit) as exc:
            await async_main(build_parser().parse_args([]))
        assert exc.value.code == 1
        conn.close.assert_awaited_once()
        assert any("pg_advisory_unlock" in c for c in self._lock_calls(conn))
