"""Shared fixtures.

Integration tests need the Compose database (``docker compose up -d db``).
Point them elsewhere with SHOP_MCP_TEST_DATABASE_URL. If the database is not
reachable they are skipped, so the pure unit tests still run anywhere.
"""

import os

import psycopg
import pytest
from mcp import Client

from shop_mcp.config import Settings
from shop_mcp.db import Database
from shop_mcp.server import create_server

TEST_DATABASE_URL = os.environ.get(
    "SHOP_MCP_TEST_DATABASE_URL",
    "postgresql://mcp_readonly:mcp_readonly_pw@127.0.0.1:55432/shop",
)


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL, **overrides)


@pytest.fixture(scope="session")
def database_available() -> None:
    try:
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"test database not reachable ({exc.__class__.__name__}); run `docker compose up -d db`")


@pytest.fixture
def db(database_available):
    database = Database(make_settings(max_rows=50, statement_timeout_ms=500))
    database.open()
    yield database
    database.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(database_available):
    """An MCP client connected to the server in memory, as a host would be."""
    server = create_server(make_settings(statement_timeout_ms=1_000))
    async with Client(server, raise_exceptions=True) as c:
        yield c
