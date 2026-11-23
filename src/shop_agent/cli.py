"""Command line: ``shop-agent "What was revenue last month?"``.

The answer goes to stdout and a short trace of the run to stderr, so the
answer can be piped on its own. Every run is also appended to the JSONL log.
"""

import argparse
import asyncio
import os
import sys

import anthropic
import httpx2
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from shop_agent.agent import AgentResult, ShopAgent, format_summary, to_json
from shop_agent.config import AgentSettings
from shop_agent.telemetry import EventLog


def mcp_server(settings: AgentSettings) -> str | StdioServerParameters:
    """What mcp.Client connects to: a URL (Streamable HTTP) or a subprocess (stdio)."""
    if settings.transport == "http":
        return settings.mcp_url

    # The child process only inherits a minimal environment, so pass the
    # server's own settings through. It also reads .env from the working
    # directory, like `uv run shop-mcp` does.
    env = {k: v for k, v in os.environ.items() if k.startswith("SHOP_MCP_")}
    # Forced: a .env meant for the Docker setup may say streamable-http, and
    # then the child would start a web server instead of talking on stdio.
    env["SHOP_MCP_TRANSPORT"] = "stdio"
    # The server logs to stderr, which is our terminal; keep it to warnings.
    env.setdefault("SHOP_MCP_LOG_LEVEL", "WARNING")
    return StdioServerParameters(command=sys.executable, args=["-m", "shop_mcp"], env=env)


def make_llm(settings: AgentSettings) -> anthropic.AsyncAnthropic:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    # None lets the SDK find credentials itself (environment, `ant` profile).
    # An empty value, as copied from .env.example, counts as not set.
    return anthropic.AsyncAnthropic(api_key=key or None)


async def ask(question: str, settings: AgentSettings, log: EventLog) -> AgentResult:
    async with make_llm(settings) as llm, Client(mcp_server(settings), read_timeout_seconds=30) as mcp:
        return await ShopAgent(llm, mcp, settings, log).run(question)


def explain_error(exc: BaseException, settings: AgentSettings) -> str | None:
    """A one-line message for the setup problems a user can fix, else None."""
    # mcp.Client runs an anyio task group, which wraps errors raised inside
    # it in an ExceptionGroup. Unwrap to the single underlying error.
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]

    # The SDK raises TypeError before sending a request without credentials.
    if isinstance(exc, anthropic.AuthenticationError) or (
        isinstance(exc, TypeError) and "authentication method" in str(exc)
    ):
        return "No valid Claude API key: set ANTHROPIC_API_KEY in the environment or in .env (see .env.example)."
    if isinstance(exc, anthropic.APIError):
        return f"Claude API error: {exc}"
    if isinstance(exc, (httpx2.ConnectError, ConnectionError)):
        return f"Cannot reach the MCP server at {settings.mcp_url}. Is it running? (docker compose up -d)"
    if isinstance(exc, MCPError):
        # Tool-level MCP errors are handled inside the loop, so this one is
        # about the connection: typically the stdio server failed to start.
        hint = " Check SHOP_MCP_DATABASE_URL and that the database is up." if settings.transport == "stdio" else ""
        return f"MCP server error: {exc}.{hint} The server's own log is above."
    return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="shop-agent", description="Ask the shop database a question in plain language.")
    p.add_argument("question")
    p.add_argument("--transport", choices=["stdio", "http"], help="default: SHOP_AGENT_TRANSPORT or stdio")
    p.add_argument("--url", dest="mcp_url", help="MCP endpoint for --transport http")
    p.add_argument("--model", help="default: SHOP_AGENT_MODEL or claude-opus-5-5")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-steps", type=int)
    p.add_argument("--json", action="store_true", help="print the full result, with tool calls, as JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="no trace on stderr")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # Command-line flags override environment variables and .env.
    overrides = {
        k: v for k in ("transport", "mcp_url", "model", "effort", "max_steps") if (v := getattr(args, k)) is not None
    }
    try:
        settings = AgentSettings(**overrides)
    except ValidationError as exc:
        sys.exit(f"Invalid settings:\n{exc}")

    log = EventLog(settings.log_file or None, echo=None if args.quiet else sys.stderr)
    try:
        result = asyncio.run(ask(args.question, settings, log))
    except Exception as exc:
        message = explain_error(exc, settings)
        if message is None:
            raise  # a bug, not a setup problem: keep the traceback
        sys.exit(message)

    if args.json:
        print(to_json(result))
    else:
        print(result.answer or f"(no answer: run stopped with {result.stop_reason})")
    if not args.quiet:
        print(format_summary(result), file=sys.stderr)
    sys.exit(0 if result.stop_reason == "end_turn" else 1)
