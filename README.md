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

# Rollback
uv run ./db_migrate.py --rollback
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
| `db_migrate.py --status` | Show applied/pending status |
| `db_migrate.py --rollback` | Rollback last applied migration |
| `db_migrate.py --create "desc"` | Generate a new migration file |
| `db_migrate.py --verbose` | Enable debug logging |

## Integration

Drop `db_migrate.py` into any project's backend directory. Configure `MIGRATIONS_DIR` to point to your migrations folder.

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

## Design Decisions

- **Single file**: Copy-paste into any project, no package installation needed
- **asyncpg**: Native async PostgreSQL driver, no ORM overhead
- **structlog**: JSON logging in production, human-readable in dev (TTY auto-detect)
- **Transactional**: Each migration runs in a single transaction
- **Schema support**: Tracking table can live in a dedicated schema via `SCHEMA_NAME`
- **dbmate-compatible**: Same `-- migrate:up` / `-- migrate:down` marker format
