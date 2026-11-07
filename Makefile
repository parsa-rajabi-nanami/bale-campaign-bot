.PHONY: setup lint typecheck test run migrate

setup:
	uv sync --locked --all-groups

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy --strict src tests

test:
	uv run pytest --cov --cov-report=term-missing

run:
	uv run python -m bale_campaign_bot.main

migrate:
	uv run python -m bale_campaign_bot.cli migrate
