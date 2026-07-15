# db_migrate

Dbmate-style async PostgreSQL migration runner. Single-file, zero-framework.

**Stack**: asyncpg + structlog + python-dotenv

## TL;DR

```bash
uv sync
export DATABASE_URL="postgresql://user:pass@localhost:5432/mydb"

# Create a migration
uv run ./db_migrate.py --create "create users table"

# Edit the generated file in db/migrations/

# Apply
uv run ./db_migrate.py

# Check status
uv run ./db_migrate.py --status

# Preview without applying
uv run ./db_migrate.py --dry-run

# Rollback last migration (or last N)
uv run ./db_migrate.py --rollback
uv run ./db_migrate.py --rollback 3

# Adopt an existing database: mark all migrations applied without running them
uv run ./db_migrate.py --baseline
```

## Migration Format

Files live in `db/migrations/` with naming `YYYYMMDDHHMMSS_description.sql`:

```sql
-- migrate:up
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- migrate:down
DROP TABLE users;
```

Each migration runs in a transaction. The `schema_migrations` table tracks applied versions.

To run a block outside a transaction (required for `CREATE INDEX CONCURRENTLY`), use the dbmate `transaction:false` marker option:

```sql
-- migrate:up transaction:false
CREATE INDEX CONCURRENTLY idx_users_name ON users (name);

-- migrate:down transaction:false
DROP INDEX CONCURRENTLY idx_users_name;
```

The version is recorded only after the block succeeds — a partial failure leaves the migration pending (clean up manually before re-running).

A `transaction:false` block must contain a **single statement**: the driver executes multi-statement strings inside an implicit transaction, which `CONCURRENTLY` refuses. Split extra statements into their own migrations.

## Configuration

Via environment variables or `.env` file:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | *(required)* | PostgreSQL connection string |
| `MIGRATIONS_DIR` | `db/migrations` | Migration files directory |
| `SCHEMA_NAME` | `public` | Schema for tracking table |

SQLAlchemy-style URLs (`postgresql+asyncpg://`) are automatically normalized.

## Commands

| Command | Description |
|---|---|
| `db_migrate.py` | Apply all pending migrations |
| `db_migrate.py --dry-run` | Show pending migrations without applying them |
| `db_migrate.py --status` | Show applied/pending status |
| `db_migrate.py --rollback [N]` | Rollback last N applied migrations (default: 1) |
| `db_migrate.py --baseline` | Mark all migrations applied without running them |
| `db_migrate.py --create "desc"` | Generate a new migration file |
| `db_migrate.py --verbose` | Enable debug logging |
| `db_migrate.py --version` | Print tool version |

## Integration

Drop `db_migrate.py` into any project's backend directory. Configure `MIGRATIONS_DIR` to point to your migrations folder.

Vendor or update from the latest release (recommended — release assets are test-gated):

```bash
curl -sL https://github.com/babs/db_migrate/releases/latest/download/db_migrate.py -o db_migrate.py
```

Check which version a vendored copy carries with `python db_migrate.py --version`.

As a dependency in `pyproject.toml`:

```toml
dependencies = [
    "asyncpg>=0.30.0",
    "structlog>=24.0.0",
    "python-dotenv>=1.0.0",
]
```

### Kubernetes Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migrate
spec:
  template:
    spec:
      containers:
        - name: migrate
          image: your-app:latest
          command: ["python", "db_migrate.py"]
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: url
      restartPolicy: OnFailure
  backoffLimit: 3
```

### Docker Compose

```yaml
services:
  migrate:
    build: .
    command: python db_migrate.py
    environment:
      DATABASE_URL: postgresql://user:pass@db:5432/mydb
    depends_on:
      db:
        condition: service_healthy
```

## Testing

```bash
uv sync
uv run pytest -m "not e2e"   # Unit tests (no Docker needed)
uv run pytest -m e2e          # E2E tests (requires Docker)
uv run pytest                  # All tests
```

E2E tests use [testcontainers](https://testcontainers.com/) to spin up a PostgreSQL instance automatically — no manual `docker-compose up` needed.

A `docker-compose.yml` is included for manual testing:

```bash
docker compose up -d
DATABASE_URL=postgresql://migrate:migrate@localhost:5432/migrate uv run ./db_migrate.py
```

## Releasing

No manual version bumps — the `release` workflow updates the files. Trigger it from the Actions UI (release → Run workflow) or:

```bash
gh workflow run release -f version=0.3.0
```

The workflow then:

1. Validates the version format and that tag `v<version>` doesn't already exist
2. Stamps `__version__` in `db_migrate.py` and `version` in `pyproject.toml` (+ `uv.lock` via `uv sync`)
3. Runs the full test suite against the stamped tree
4. Commits `chore(release): v<version>`, tags it, pushes both
5. Publishes a GitHub Release with `db_migrate.py` attached and auto-generated notes

Versions with a suffix (e.g. `0.3.0-rc1`) are marked as pre-releases. A validation or test failure aborts before anything is pushed.

If a run dies between the tag push and the release publication, re-running the workflow with the same version enters recovery mode: it detects the existing tag, skips stamping/tests/push, and just publishes the release from the tagged commit. A version whose tag *and* release both exist is rejected.

## Design Decisions

- **Single file**: Copy-paste into any project, no package installation needed
- **asyncpg**: Native async PostgreSQL driver, no ORM overhead
- **structlog**: JSON logging in production, human-readable in dev (TTY auto-detect)
- **Transactional**: Each migration runs in a single transaction
- **Concurrency-safe**: A PostgreSQL advisory lock serializes concurrent runners (e.g. parallel k8s Jobs) — the second waits instead of racing
- **Schema support**: Tracking table can live in a dedicated schema via `SCHEMA_NAME`
- **dbmate-compatible**: Same `-- migrate:up` / `-- migrate:down` marker format, including the `transaction:false` block option
