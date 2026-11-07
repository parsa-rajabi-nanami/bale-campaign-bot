"""Async engine, session factory, and SQLite path preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    options: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    if database_url.endswith(":memory:"):
        options["poolclass"] = StaticPool
    engine = create_async_engine(database_url, **options)
    if database_url.startswith("sqlite+aiosqlite:"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(connection: Any, _: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def ensure_sqlite_parent(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite URL if needed."""
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url[len(prefix) :].split("?", maxsplit=1)[0]
    if not raw_path or raw_path == ":memory:":
        return
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
