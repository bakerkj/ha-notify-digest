.PHONY: venv test lint clean

venv:
	uv sync --all-groups

test:
	uv run python -m pytest tests/ -v

lint:
	SKIP=no-commit-to-branch uv run pre-commit run --all-files

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache __pycache__ tests/__pycache__
