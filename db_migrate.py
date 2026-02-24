#!/usr/bin/env python3
"""Dbmate-style async PostgreSQL migration runner.

Single-file, zero-framework migration tool using asyncpg and structlog.

Usage:
    db_migrate.py                          # Apply pending migrations
    db_migrate.py --status                 # Show migration status
    db_migrate.py --rollback               # Rollback last applied migration
    db_migrate.py --create "add users"     # Create new migration file
    db_migrate.py --verbose                # Enable debug logging

Migration files: YYYYMMDDHHMMSS_description.sql

    -- migrate:up
    CREATE TABLE ...;

    -- migrate:down
    DROP TABLE ...;

Environment:
    DATABASE_URL       PostgreSQL connection string (required)
    MIGRATIONS_DIR     Path to migrations directory (default: ./db/migrations)
    SCHEMA_NAME        PostgreSQL schema for tracking table (default: public)
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger()

_VALID_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

MIGRATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
    version VARCHAR(14) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class MigrationError(Exception):
    """Raised when a migration operation fails."""


def validate_schema_name(schema: str) -> None:
    """Validate schema is a safe PostgreSQL identifier."""
    if not _VALID_IDENTIFIER.match(schema):
        raise MigrationError(f"invalid schema name: {schema!r}")


def setup_logging(verbose: bool = False) -> None:
    is_tty = sys.stdout.isatty()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if is_tty else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if verbose else logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def normalize_database_url(url: str) -> str:
    """Strip SQLAlchemy driver suffixes so asyncpg can connect."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


def parse_migration(content: str, direction: str = "up") -> str | None:
    """Extract SQL block for the given direction from a migration file."""
    pattern = rf"--\s*migrate:{direction}\s*\n(.*?)(?=--\s*migrate:|$)"
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    sql = match.group(1).strip()
    return sql or None


def discover_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Return sorted (version, path) pairs from the migrations directory."""
    if not migrations_dir.exists():
        log.warning("migrations_dir_missing", path=str(migrations_dir))
        return []

    migrations: list[tuple[str, Path]] = []
    for f in sorted(migrations_dir.glob("*.sql")):
        match = re.match(r"^(\d{14})_", f.name)
        if match:
            migrations.append((match.group(1), f))
        else:
            log.warning("skipping_invalid_filename", file=f.name)
    return migrations


def create_migration(migrations_dir: Path, description: str) -> Path:
    """Create a new empty migration file and return its path."""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", description.lower()).strip("_")
    filename = f"{timestamp}_{slug}.sql"
    path = migrations_dir / filename
    path.write_text("-- migrate:up\n\n\n-- migrate:down\n\n")
    log.info("migration_created", file=filename)
    return path


async def ensure_schema_and_table(conn: asyncpg.Connection, schema: str) -> None:
    validate_schema_name(schema)
    if schema != "public":
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    await conn.execute(MIGRATIONS_TABLE_DDL.format(schema=schema))


async def get_applied_versions(conn: asyncpg.Connection, schema: str) -> set[str]:
    validate_schema_name(schema)
    rows = await conn.fetch(f"SELECT version FROM {schema}.schema_migrations ORDER BY version")
    return {r["version"] for r in rows}


async def run_migrate(conn: asyncpg.Connection, schema: str, migrations_dir: Path) -> int:
    """Apply all pending migrations. Returns count applied."""
    await ensure_schema_and_table(conn, schema)
    applied = await get_applied_versions(conn, schema)
    all_migrations = discover_migrations(migrations_dir)
    pending = [(v, p) for v, p in all_migrations if v not in applied]

    if not pending:
        log.info("no_pending_migrations")
        return 0

    log.info("pending_migrations_found", count=len(pending))
    for version, path in pending:
        content = path.read_text()
        up_sql = parse_migration(content, "up")
        if not up_sql:
            raise MigrationError(f"no -- migrate:up block in {path.name}")

        log.info("applying_migration", file=path.name)
        async with conn.transaction():
            await conn.execute(up_sql)
            await conn.execute(f"INSERT INTO {schema}.schema_migrations (version) VALUES ($1)", version)

    log.info("migrations_applied", count=len(pending))
    return len(pending)


async def run_rollback(conn: asyncpg.Connection, schema: str, migrations_dir: Path) -> bool:
    """Rollback the most recently applied migration. Returns True on success."""
    await ensure_schema_and_table(conn, schema)

    row = await conn.fetchrow(f"SELECT version FROM {schema}.schema_migrations ORDER BY version DESC LIMIT 1")
    if not row:
        log.info("nothing_to_rollback")
        return False

    version = row["version"]
    all_migrations = discover_migrations(migrations_dir)
    path = next((p for v, p in all_migrations if v == version), None)
    if not path:
        raise MigrationError(f"migration file for version {version} not found on disk")

    content = path.read_text()
    down_sql = parse_migration(content, "down")
    if not down_sql:
        raise MigrationError(f"no -- migrate:down block in {path.name}")

    log.info("rolling_back", file=path.name)
    async with conn.transaction():
        await conn.execute(down_sql)
        await conn.execute(f"DELETE FROM {schema}.schema_migrations WHERE version = $1", version)
    log.info("rolled_back", file=path.name)
    return True


async def run_status(conn: asyncpg.Connection, schema: str, migrations_dir: Path) -> None:
    """Print migration status table."""
    await ensure_schema_and_table(conn, schema)
    applied = await get_applied_versions(conn, schema)
    all_migrations = discover_migrations(migrations_dir)

    if not all_migrations:
        print(f"No migration files found in {migrations_dir}")
        return

    pending_count = 0
    for version, path in all_migrations:
        marker = "applied" if version in applied else "pending"
        if marker == "pending":
            pending_count += 1
        print(f"  [{marker:>7}]  {path.name}")

    print()
    if pending_count:
        print(f"  {pending_count} pending migration(s)")
    else:
        print("  All migrations applied.")


async def async_main(args: argparse.Namespace) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        log.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    database_url = normalize_database_url(database_url)
    schema = os.environ.get("SCHEMA_NAME", "public")
    migrations_dir = Path(os.environ.get("MIGRATIONS_DIR", "db/migrations"))

    try:
        validate_schema_name(schema)
    except MigrationError:
        log.error("invalid_schema_name", schema=schema)
        sys.exit(1)

    conn = await asyncpg.connect(database_url, timeout=30)
    try:
        if args.status:
            await run_status(conn, schema, migrations_dir)
        elif args.rollback:
            await run_rollback(conn, schema, migrations_dir)
        else:
            await run_migrate(conn, schema, migrations_dir)
    except MigrationError as e:
        log.error("migration_failed", error=str(e))
        sys.exit(1)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Async PostgreSQL migration runner")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    parser.add_argument("--rollback", action="store_true", help="Rollback last applied migration")
    parser.add_argument("--create", metavar="DESC", help="Create a new migration file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    setup_logging(verbose=args.verbose)

    if args.create:
        migrations_dir = Path(os.environ.get("MIGRATIONS_DIR", "db/migrations"))
        create_migration(migrations_dir, args.create)
        return

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
