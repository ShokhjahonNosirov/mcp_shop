"""Runtime configuration, read from environment variables.

Every setting uses the ``SHOP_MCP_`` prefix. That is deliberate: a developer
machine often has a generic ``DATABASE_URL`` pointing at an owner/admin role,
and we never want this server to pick that up by accident. It must be given
the read-only role's URL explicitly.
"""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHOP_MCP_",
        env_file=".env",
        extra="ignore",
    )

    # SecretStr keeps the password out of reprs, logs and tracebacks.
    database_url: SecretStr = Field(
        description="libpq URL for the read-only role, e.g. postgresql://mcp_readonly:...@host:5432/shop",
    )

    # Limits applied to every query the server runs.
    max_rows: int = Field(default=200, ge=1, le=10_000)
    statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)

    # Connection pool sizing. Agents make few, short queries, so keep it small.
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=5, ge=1)

    # How the MCP server is exposed: stdio (host spawns us) or streamable HTTP.
    transport: Literal["stdio", "streamable-http"] = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=1, le=65_535)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
