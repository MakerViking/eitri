import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeMoonraker  # noqa: E402


@pytest_asyncio.fixture
async def moonraker():
    server = await FakeMoonraker().start()
    yield server
    await server.stop()
