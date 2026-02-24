.PHONY: install lint test test-unit test-e2e migrate status rollback clean

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

status:
	uv run python db_migrate.py --status

rollback:
	uv run python db_migrate.py --rollback

clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov
