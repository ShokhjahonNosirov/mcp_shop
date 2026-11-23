"""Agent settings, read from environment variables (and .env).

Agent settings use the ``SHOP_AGENT_`` prefix, separate from the server's
``SHOP_MCP_`` settings: the agent is a client and never sees the database URL.
The API key is the standard ``ANTHROPIC_API_KEY``. It is optional here
because the SDK also finds it in the environment on its own; reading it
here as well means a key in .env works too.
"""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHOP_AGENT_",
        env_file=".env",
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = Field(
        default=None,
        # An explicit alias is used as-is, without the SHOP_AGENT_ prefix.
        validation_alias=AliasChoices("ANTHROPIC_API_KEY"),
    )

    model: str = "claude-opus-5-5"
    # How hard the model thinks before answering. Lower is cheaper and faster.
    # Set explicitly because the default differs between models.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = Field(default=16_000, ge=256)

    # One step is one model call. A normal question needs 2-4; the limit stops
    # a confused model from looping (and spending) forever.
    max_steps: int = Field(default=10, ge=1, le=50)

    # Tool results are cut to this many characters before they go to the
    # model, so one large query result cannot flood the context (and the bill).
    max_tool_result_chars: int = Field(default=20_000, ge=1_000)

    # If Claude declines a request (stop_reason "refusal"), let the API retry
    # it on a substitute model inside the same call. Off: the run just ends.
    refusal_fallback: bool = True

    # stdio: start the server as a subprocess (only the database must run).
    # http: connect to a running server, e.g. the Docker Compose one.
    transport: Literal["stdio", "http"] = "stdio"
    mcp_url: str = "http://127.0.0.1:8000/mcp"

    # One JSON object per line. Empty string disables the file.
    log_file: str = "logs/agent.jsonl"
