.PHONY: install lint test test-unit test-e2e migrate dry-run status rollback baseline clean

install:
	uv sync

lint:
	pre-commit run --all-files

test:
	uv run pytest

test-unit:
	uv run pytest -m "not e2e"

test-e2e:
	uv run pytest -m e2e

migrate:
	uv run python db_migrate.py

dry-run:
	uv run python db_migrate.py --dry-run

status:
	uv run python db_migrate.py --status

rollback:
	uv run python db_migrate.py --rollback

baseline:
	uv run python db_migrate.py --baseline

clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov
