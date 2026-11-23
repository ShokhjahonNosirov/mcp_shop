"""The agent loop, without calling the Claude API.

The Anthropic client is replaced by FakeLLM, which returns scripted responses
(real SDK message objects) and records every request. The MCP side is real:
a small in-memory MCP server with shop-like tools, so tool conversion and
error handling go through the actual MCP client. One test at the end uses
the real shop server and is skipped without the database.
"""

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from shop_agent import cli
from shop_agent.agent import AgentResult, ShopAgent, mcp_tool_to_claude
from shop_agent.config import AgentSettings
from shop_agent.pricing import Usage, estimate_cost
from shop_agent.telemetry import EventLog, read_events

pytestmark = pytest.mark.anyio

TODAY = date(2026, 11, 23)


# --- Fakes --------------------------------------------------------------------


def fake_shop_server() -> MCPServer:
    server = MCPServer("fake-shop", instructions="Revenue counts paid, shipped and delivered orders.")

    @server.tool()
    def revenue_summary(start_date: date, end_date: date) -> dict:
        """Revenue between two dates."""
        return {"start_date": str(start_date), "end_date": str(end_date), "revenue": "1234.50"}

    @server.tool()
    def find_customers(search: str) -> dict:
        """Look up a customer by name."""
        if search != "Maya":
            raise ToolError(f"No customer matches {search!r}.")
        return {"customers": [{"id": 2, "full_name": "Maya Schmidt"}]}

    @server.tool()
    def run_select_query(sql: str) -> dict:
        """Run one SELECT."""
        return {"rows": [{"x": "y" * 5000}]}

    return server


class FakeLLM:
    """Stands in for anthropic.AsyncAnthropic: scripted replies, recorded requests."""

    def __init__(self, *responses: BetaMessage) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> BetaMessage:
        # Copy the list: the agent keeps appending to it after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        assert self.responses, "the model was called more often than scripted"
        return self.responses.pop(0)


def reply(*content: dict, stop_reason: str | None = None, input_tokens=1000, output_tokens=100, **usage) -> BetaMessage:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, **usage},
            # Thinking always comes first on current models; it must be passed back.
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, *content],
        }
    )


def text(t: str) -> dict:
    return {"type": "text", "text": t}


_ids = iter(range(1, 1_000))


def tool_use(name: str, **arguments: Any) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": arguments}


@pytest.fixture
async def fake_mcp():
    async with Client(fake_shop_server()) as client:
        yield client


@pytest.fixture
def make_agent(tmp_path):
    def make(llm: FakeLLM, mcp: Client, **settings: Any) -> ShopAgent:
        config = AgentSettings(_env_file=None, log_file=str(tmp_path / "agent.jsonl"), **settings)
        return ShopAgent(llm, mcp, config, EventLog(config.log_file), today=lambda: TODAY)

    make.log_path = tmp_path / "agent.jsonl"
    return make


def tool_results(request: dict) -> list[dict]:
    """The tool_result blocks in the last message of a request."""
    return request["messages"][-1]["content"]


# --- Tool conversion ------------------------------------------------------------


async def test_mcp_tools_become_claude_tools(fake_mcp):
    tools = {t.name: mcp_tool_to_claude(t) for t in (await fake_mcp.list_tools()).tools}
    revenue = tools["revenue_summary"]
    assert set(revenue) == {"name", "description", "input_schema"}
    assert revenue["description"] == "Revenue between two dates."
    assert revenue["input_schema"]["type"] == "object"
    assert revenue["input_schema"]["required"] == ["start_date", "end_date"]
    assert revenue["input_schema"]["properties"]["start_date"]["format"] == "date"


# --- The loop -------------------------------------------------------------------


async def test_answers_after_a_tool_call(fake_mcp, make_agent):
    call = tool_use("revenue_summary", start_date="2026-10-01", end_date="2026-10-31")
    llm = FakeLLM(
        reply(text("Let me check."), call, input_tokens=2000, output_tokens=150),
        reply(text("Revenue in October was €1,234.50."), input_tokens=2500, output_tokens=40),
    )
    result = await make_agent(llm, fake_mcp).run("What was revenue last month?")

    assert result.stop_reason == "end_turn"
    assert result.answer == "Revenue in October was €1,234.50."
    assert result.steps == 2
    assert [(c.name, c.arguments, c.is_error) for c in result.tool_calls] == [
        ("revenue_summary", {"start_date": "2026-10-01", "end_date": "2026-10-31"}, False)
    ]
    assert result.usage == Usage(input_tokens=4500, output_tokens=190)
    assert result.cost_usd == estimate_cost("claude-opus-5-5", result.usage)

    first, second = llm.requests
    assert {t["name"] for t in first["tools"]} == {"revenue_summary", "find_customers", "run_select_query"}
    assert "2026-11-23" in first["system"]
    assert "Revenue counts paid, shipped and delivered orders." in first["system"]  # server instructions
    assert first["messages"] == [{"role": "user", "content": "What was revenue last month?"}]

    # Second request: the assistant turn is passed back whole (thinking
    # included), followed by the tool result for the same tool_use id.
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert [b.type for b in assistant["content"]] == ["thinking", "text", "tool_use"]
    [result_block] = tool_results(second)
    assert result_block["tool_use_id"] == call["id"]
    assert result_block["is_error"] is False
    assert '"revenue": "1234.50"' in result_block["content"]


async def test_request_settings(fake_mcp, make_agent):
    llm = FakeLLM(reply(text("Hi.")))
    await make_agent(llm, fake_mcp, model="claude-sonnet-5-5", effort="low", max_tokens=4000).run("hi")
    [request] = llm.requests
    assert request["model"] == "claude-sonnet-5-5"
    assert request["max_tokens"] == 4000
    assert request["output_config"] == {"effort": "low"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["fallbacks"] == "default"


async def test_refusal_fallback_can_be_turned_off(fake_mcp, make_agent):
    llm = FakeLLM(reply(text("Hi.")))
    await make_agent(llm, fake_mcp, refusal_fallback=False).run("hi")
    assert "fallbacks" not in llm.requests[0]
    assert "betas" not in llm.requests[0]


async def test_tool_error_goes_back_to_the_model_which_retries(fake_mcp, make_agent):
    llm = FakeLLM(
        reply(tool_use("find_customers", search="Maya Schmidt")),
        reply(tool_use("find_customers", search="Maya")),
        reply(text("Maya Schmidt is customer #2.")),
    )
    result = await make_agent(llm, fake_mcp).run("Who is Maya Schmidt?")

    [error] = tool_results(llm.requests[1])
    assert error["is_error"] is True
    assert "No customer matches 'Maya Schmidt'." in error["content"]
    assert [c.is_error for c in result.tool_calls] == [True, False]
    assert result.answer == "Maya Schmidt is customer #2."


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("revenue_summary", {"start_date": "2026-10-01"}, "Field required"),
        ("revenue_summary", {"start_date": "yesterday", "end_date": "2026-10-31"}, "valid date"),
        ("drop_tables", {}, "Unknown tool"),
    ],
)
async def test_invalid_calls_are_returned_as_errors(fake_mcp, make_agent, name, arguments, message):
    llm = FakeLLM(reply(tool_use(name, **arguments)), reply(text("Sorry.")))
    result = await make_agent(llm, fake_mcp).run("q")
    [error] = tool_results(llm.requests[1])
    assert error["is_error"] is True
    assert message in error["content"]
    assert result.stop_reason == "end_turn"


async def test_parallel_tool_calls_are_answered_in_one_message(fake_mcp, make_agent):
    a = tool_use("revenue_summary", start_date="2026-09-01", end_date="2026-09-30")
    b = tool_use("revenue_summary", start_date="2026-10-01", end_date="2026-10-31")
    llm = FakeLLM(reply(a, b), reply(text("Both months: €1,234.50.")))
    result = await make_agent(llm, fake_mcp).run("September vs October?")

    results = tool_results(llm.requests[1])
    assert [r["tool_use_id"] for r in results] == [a["id"], b["id"]]
    assert len(result.tool_calls) == 2
    assert {c.step for c in result.tool_calls} == {1}


async def test_stops_after_max_steps(fake_mcp, make_agent):
    looping = [reply(tool_use("find_customers", search="Maya")) for _ in range(3)]
    llm = FakeLLM(*looping)
    result = await make_agent(llm, fake_mcp, max_steps=3).run("q")

    assert result.stop_reason == "max_steps"
    assert result.answer == ""
    assert len(llm.requests) == 3
    assert len(result.tool_calls) == 3


async def test_refusal_ends_the_run(fake_mcp, make_agent):
    llm = FakeLLM(reply(stop_reason="refusal"))
    result = await make_agent(llm, fake_mcp).run("q")
    assert result.stop_reason == "refusal"
    assert result.tool_calls == []


async def test_large_tool_results_are_truncated(fake_mcp, make_agent):
    llm = FakeLLM(reply(tool_use("run_select_query", sql="SELECT 1")), reply(text("Done.")))
    await make_agent(llm, fake_mcp, max_tool_result_chars=1000).run("q")
    [block] = tool_results(llm.requests[1])
    assert len(block["content"]) < 1100
    assert "[truncated: result was" in block["content"]


async def test_tools_are_listed_once_per_connection(fake_mcp, make_agent):
    llm = FakeLLM(reply(text("a")), reply(text("b")))
    agent = make_agent(llm, fake_mcp)
    await agent.run("one")
    tools = agent._tools
    await agent.run("two")
    assert agent._tools is tools


# --- Monitoring -------------------------------------------------------------------


async def test_every_run_is_logged_as_json_lines(fake_mcp, make_agent):
    llm = FakeLLM(
        reply(tool_use("find_customers", search="Nobody"), input_tokens=900, output_tokens=60),
        reply(text("No such customer."), input_tokens=1000, output_tokens=20, cache_read_input_tokens=800),
    )
    result = await make_agent(llm, fake_mcp).run("Find Nobody")

    events = read_events(make_agent.log_path)
    assert [e["event"] for e in events] == ["run_start", "llm_call", "tool_call", "llm_call", "run_end"]
    assert {e["run_id"] for e in events} == {result.run_id}

    tool_call = events[2]
    assert tool_call["tool"] == "find_customers"
    assert tool_call["arguments"] == {"search": "Nobody"}
    assert tool_call["is_error"] is True
    assert "No customer matches" in tool_call["error"]
    assert isinstance(tool_call["duration_ms"], int)

    end = events[-1]
    assert end["input_tokens"] == 1900
    assert end["output_tokens"] == 80
    assert end["cache_read_input_tokens"] == 800
    assert end["tool_calls"] == 1
    assert end["tool_errors"] == 1
    assert end["used_sql_tool"] is False
    assert end["cost_usd"] == result.cost_usd > 0
    assert end["answer"] == "No such customer."


async def test_api_errors_are_logged_then_raised(fake_mcp, make_agent):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    class FailingLLM(FakeLLM):
        async def _create(self, **kwargs):
            raise anthropic.APIConnectionError(request=request)

    with pytest.raises(anthropic.APIConnectionError):
        await make_agent(FailingLLM(), fake_mcp).run("q")
    end = read_events(make_agent.log_path)[-1]
    assert end["event"] == "run_end"
    assert end["stop_reason"] == "error"
    assert end["steps"] == 1
    assert "APIConnectionError" in end["error"]


async def test_a_failure_before_the_first_model_call_still_ends_the_run_in_the_log(make_agent):
    class BrokenMCP:
        instructions = None

        async def list_tools(self):
            raise ConnectionResetError("server went away")

    with pytest.raises(ConnectionResetError):
        await make_agent(FakeLLM(), BrokenMCP()).run("q")
    events = read_events(make_agent.log_path)
    assert [e["event"] for e in events] == ["run_start", "run_end"]
    assert events[-1]["stop_reason"] == "error"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TypeError('"Could not resolve authentication method. Expected one of api_key"'), "ANTHROPIC_API_KEY"),
        (ExceptionGroup("tg", [ConnectionRefusedError()]), "Cannot reach the MCP server at http://127.0.0.1:8000/mcp"),
        (ExceptionGroup("tg", [ValueError("a bug")]), None),
    ],
)
def test_cli_explains_setup_errors(exc, expected):
    message = cli.explain_error(exc, AgentSettings(_env_file=None))
    if expected is None:
        assert message is None  # unknown errors keep their traceback
    else:
        assert expected in message


def test_cost_estimate():
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=200_000,
        cache_read_input_tokens=500_000,
    )
    # 4.00 input + 2.00 output + 0.2 * 4.00 * 1.25 cache write + 0.5 * 0.20 cache read
    assert estimate_cost("claude-opus-5-5", usage) == pytest.approx(4.00 + 2.00 + 1.00 + 0.10)
    assert estimate_cost("some-future-model", usage) is None


# --- Configuration and CLI ------------------------------------------------------


def test_settings_defaults_and_env(monkeypatch):
    for var in ("SHOP_AGENT_MODEL", "SHOP_AGENT_MAX_STEPS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    defaults = AgentSettings(_env_file=None)
    assert defaults.model == "claude-opus-5-5"
    assert defaults.transport == "stdio"
    assert defaults.anthropic_api_key is None

    monkeypatch.setenv("SHOP_AGENT_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("SHOP_AGENT_MAX_STEPS", "4")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    settings = AgentSettings(_env_file=None)
    assert (settings.model, settings.max_steps) == ("claude-haiku-5-5", 4)
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-test-secret"
    assert "sk-ant-test-secret" not in repr(settings)


def test_stdio_server_gets_only_its_own_settings(monkeypatch):
    monkeypatch.setenv("SHOP_MCP_DATABASE_URL", "postgresql://mcp_readonly:pw@127.0.0.1:55432/shop")
    monkeypatch.setenv("SHOP_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    server = cli.mcp_server(AgentSettings(_env_file=None, transport="stdio"))

    assert isinstance(server, StdioServerParameters)
    assert server.args == ["-m", "shop_mcp"]
    assert server.env["SHOP_MCP_DATABASE_URL"].startswith("postgresql://mcp_readonly")
    assert server.env["SHOP_MCP_TRANSPORT"] == "stdio"
    # The server process never sees the Claude API key.
    assert "ANTHROPIC_API_KEY" not in server.env


def test_http_server_is_a_url():
    settings = AgentSettings(_env_file=None, transport="http", mcp_url="http://127.0.0.1:8000/mcp")
    assert cli.mcp_server(settings) == "http://127.0.0.1:8000/mcp"


def test_cli_prints_the_answer_and_applies_flags(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SHOP_AGENT_LOG_FILE", str(tmp_path / "cli.jsonl"))
    seen = {}

    async def fake_ask(question, settings, log):
        seen.update(question=question, settings=settings)
        return AgentResult("r1", question, "42 orders.", "end_turn", 2, [], Usage(10, 5), 1200, 0.001, settings.model)

    monkeypatch.setattr(cli, "ask", fake_ask)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["How many orders?", "--max-steps", "3", "--model", "claude-sonnet-5-5"])

    assert exit_info.value.code == 0
    assert seen["question"] == "How many orders?"
    assert (seen["settings"].max_steps, seen["settings"].model) == (3, "claude-sonnet-5-5")
    out, err = capsys.readouterr()
    assert out.strip() == "42 orders."
    assert "[end_turn] 2 steps" in err


def test_cli_json_output(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SHOP_AGENT_LOG_FILE", str(tmp_path / "cli.jsonl"))

    async def fake_ask(question, settings, log):
        return AgentResult("r1", question, "", "max_steps", 10, [], Usage(), 5, None, settings.model)

    monkeypatch.setattr(cli, "ask", fake_ask)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["q", "--json", "--quiet"])
    assert exit_info.value.code == 1  # did not answer
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "max_steps"


# --- Against the real shop server (needs the database) ----------------------------


async def test_agent_calls_the_real_shop_server(client, make_agent):
    llm = FakeLLM(
        reply(tool_use("top_products", period="last_30_days", limit=3)),
        reply(text("The best seller is ...")),
    )
    result = await make_agent(llm, client).run("What sold best this month?")

    [call] = result.tool_calls
    assert call.is_error is False
    assert len(json.loads(call.result)["products"]) == 3
    assert "Revenue only" in llm.requests[0]["system"]  # the real server's instructions
    assert len(llm.requests[0]["tools"]) == 7
