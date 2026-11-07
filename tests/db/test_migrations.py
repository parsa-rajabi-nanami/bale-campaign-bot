from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from bale_campaign_bot.db.session import create_engine

ROOT = Path(__file__).resolve().parents[2]


def test_fresh_sqlite_database_migrates_to_head(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    command.check(config)

    async def table_names() -> set[str]:
        engine = create_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: set(inspect(sync_connection).get_table_names())
                )
        finally:
            await engine.dispose()

    names = asyncio.run(table_names())
    assert {
        "admins",
        "bot_state",
        "campaigns",
        "campaign_recipients",
        "conversation_states",
        "processed_updates",
        "scheduler_leases",
        "subscribers",
        "subscriber_tags",
        "tags",
        "alembic_version",
    } <= names
