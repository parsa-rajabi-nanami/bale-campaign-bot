from collections.abc import Iterator

import pytest
import respx


@pytest.fixture
def fake_bale_server() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url="https://tapi.bale.ai/botTEST_TOKEN/") as router:
        yield router
