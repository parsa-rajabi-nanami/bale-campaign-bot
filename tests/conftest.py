from collections.abc import AsyncIterator, Iterator

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.models import Base
from bale_campaign_bot.db.session import create_engine, create_session_factory


@pytest.fixture
def fake_bale_server() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url="https://tapi.bale.ai/botTEST_TOKEN/") as router:
        yield router


@pytest.fixture
async def test_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()
