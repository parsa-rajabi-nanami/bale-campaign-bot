# Contributing

Contributions should preserve opt-in-only messaging and the Bale API constraints in `docs/BALE_API_NOTES.md`. Do not add Telegram libraries or assume Telegram-compatible request fields.

## Development setup

- Python 3.12 and uv
- `uv sync --locked --all-groups`
- No live Bale token is needed for tests; HTTP behavior is mocked with respx.

## Before opening a pull request

Run all phase checks:

```sh
make lint
make typecheck
make test
uv run pip-audit
```

Add deterministic tests for failure cases. Keep tests offline. Add an Alembic migration for schema changes and verify a fresh database reaches head. Update English and Persian operator documentation when behavior or configuration changes.

## Code conventions

- Keep HTTP calls in `bale/`, persistence in repositories, business rules in services, and handlers thin.
- Use type hints and keep `mypy --strict` clean.
- Inject clocks for time-dependent logic; persist UTC.
- Never log tokens, secrets, phone lists, or full campaign bodies.
- Imported phone lists may only match existing linked opt-in subscribers. They must not subscribe or message anyone.
- Follow conventional, focused commit messages. Do not include real tokens, user data, or business data in commits or fixtures.
