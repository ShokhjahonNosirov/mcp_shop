import pytest
from pydantic import ValidationError

from shop_mcp.config import Settings

URL = "postgresql://mcp_readonly:secret@localhost:5432/shop"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Isolate tests from the developer's real environment.
    import os

    for key in list(os.environ):
        if key.startswith("SHOP_MCP_"):
            monkeypatch.delenv(key)


def load(**env: str) -> Settings:
    # _env_file=None: never read a local .env during tests.
    return Settings(_env_file=None, **env)


def test_defaults(monkeypatch):
    monkeypatch.setenv("SHOP_MCP_DATABASE_URL", URL)
    s = load()
    assert s.max_rows == 200
    assert s.statement_timeout_ms == 5_000
    assert s.transport == "stdio"


def test_reads_prefixed_env(monkeypatch):
    monkeypatch.setenv("SHOP_MCP_DATABASE_URL", URL)
    monkeypatch.setenv("SHOP_MCP_MAX_ROWS", "50")
    monkeypatch.setenv("SHOP_MCP_TRANSPORT", "streamable-http")
    s = load()
    assert s.max_rows == 50
    assert s.transport == "streamable-http"


def test_ignores_unprefixed_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:pw@localhost/shop")
    with pytest.raises(ValidationError):
        load()


def test_password_not_leaked_in_repr(monkeypatch):
    monkeypatch.setenv("SHOP_MCP_DATABASE_URL", URL)
    assert "secret" not in repr(load())


@pytest.mark.parametrize("value", ["0", "100000"])
def test_rejects_out_of_range_limits(monkeypatch, value):
    monkeypatch.setenv("SHOP_MCP_DATABASE_URL", URL)
    monkeypatch.setenv("SHOP_MCP_MAX_ROWS", value)
    with pytest.raises(ValidationError):
        load()
