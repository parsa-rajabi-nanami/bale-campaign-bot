# Decisions

## Stack and storage

The implementation follows the requested stack: Python 3.12, `uv`, an async hand-written `httpx` Bale client, SQLAlchemy 2.x async, Alembic, SQLite by default, PostgreSQL via `asyncpg`, Pydantic v2/settings, PyYAML, structlog, pytest/respx, Ruff, and strict mypy.

SQLAlchemy's `asyncio` extra is required because its async engine depends on `greenlet`; this is declared explicitly. SQLite foreign-key enforcement is enabled on each connection so the migration's cascade/restrict rules work locally as they do in PostgreSQL.

## Bale-specific choices

- Broadcast payloads use `copyMessage` to preserve the source message's Bale formatting and media. The API request is not assumed to support attaching inline CTA markup to a copied message; CTA delivery is a separate message.
- Rate limits have no stable published numeric contract. The client retries HTTP/API 429s, honors valid `parameters.retry_after` or `Retry-After` hints, and otherwise applies bounded exponential backoff with jitter.
- API methods are called with JSON POST. Bale supports this request encoding and the client keeps request construction in one module.
- Bot tokens are held as `SecretStr` in settings, never logged by this package, and removed from API descriptions/parameters before exceptions are raised.

## Scope boundaries

Phone import is for matching only. It must never create subscribers or send to unmatched numbers. The owner claim is a private-chat one-time flow; `ADMIN_IDS` is an additive allowlist.
