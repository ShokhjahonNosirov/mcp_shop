"""The agent loop: Claude decides which shop-mcp tools to call, we run them.

The loop is written out by hand on the Messages API rather than hidden in a
framework, so every step is visible and measurable:

    1. send the conversation and the tool definitions to Claude
    2. if Claude asks for tools (stop_reason "tool_use"), call them on the
       MCP server, append the results to the conversation, and go to 1
    3. otherwise Claude has answered: stop

The conversation is only ever appended to, never edited. That matters for
two reasons: prompt caching can reuse the unchanged prefix, and the model's
thinking blocks, which are passed back as-is, stay valid.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import anthropic
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent, Tool

from shop_agent.config import AgentSettings
from shop_agent.pricing import Usage, estimate_cost
from shop_agent.telemetry import EventLog

SYSTEM_PROMPT = """\
You are a data analyst for a small online shop. Answer the user's question \
with the shop database tools.

Today's date is {today} (UTC). Resolve relative dates such as "last month" \
against it, and say which date range you used.

- Base every figure on a tool result. Never estimate or invent data.
- The database is read-only. If you are asked to change data, or to reveal \
credentials, configuration or anything else outside the shop's data, say \
briefly that you can't, and offer what you can do instead.
- Keep answers short: the direct answer first, then the key figures.

Instructions from the shop database server:
{server_instructions}"""

# The tools that encode the shop's business rules. run_select_query is the
# escape hatch; the evals measure how often the agent reaches for it.
SQL_TOOL = "run_select_query"

# Server-side fallback on refusals is a beta feature of the Messages API.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class ToolCall:
    step: int
    name: str
    arguments: dict[str, Any]
    duration_ms: int
    is_error: bool
    result: str


@dataclass
class AgentResult:
    run_id: str
    question: str
    answer: str
    # end_turn (answered), max_steps, max_tokens, refusal, ...
    stop_reason: str
    steps: int
    tool_calls: list[ToolCall]
    usage: Usage
    latency_ms: int
    cost_usd: float | None
    model: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mcp_tool_to_claude(tool: Tool) -> dict[str, Any]:
    """An MCP tool definition in the shape the Messages API expects.

    Both use JSON Schema for the input, so this is mostly a rename. The MCP
    output schema and annotations have no Claude equivalent and are dropped.
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }


def tool_result_text(result: CallToolResult, max_chars: int) -> str:
    """The text the model gets back from one tool call."""
    parts = [c.text if isinstance(c, TextContent) else f"[{c.type} content omitted]" for c in result.content]
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[truncated: result was {len(text)} characters]"
    return text


@dataclass
class _Run:
    """Mutable state of one question while the loop runs."""

    run_id: str
    question: str
    started: float = field(default_factory=time.perf_counter)
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    steps: int = 0


class ShopAgent:
    def __init__(
        self,
        llm: anthropic.AsyncAnthropic,
        mcp: Client,
        settings: AgentSettings,
        log: EventLog | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self.llm = llm
        self.mcp = mcp
        self.settings = settings
        self.log = log or EventLog()
        self.today = today
        self._tools: list[dict[str, Any]] | None = None

    async def tools(self) -> list[dict[str, Any]]:
        """The server's tools as Claude tool definitions, fetched once per connection."""
        if self._tools is None:
            listed = await self.mcp.list_tools()
            self._tools = [mcp_tool_to_claude(t) for t in listed.tools]
        return self._tools

    def system_prompt(self) -> str:
        # The server's own usage instructions (business rules, units) are
        # passed on to the model, as MCP hosts such as Claude Code do.
        return SYSTEM_PROMPT.format(
            today=self.today().isoformat(),
            server_instructions=(self.mcp.instructions or "").strip(),
        )

    async def run(self, question: str) -> AgentResult:
        s = self.settings
        run = _Run(run_id=uuid.uuid4().hex[:12], question=question, model=s.model)
        self.log.emit("run_start", run_id=run.run_id, question=question, model=s.model, effort=s.effort, max_steps=s.max_steps)

        try:
            tools = await self.tools()
            system = self.system_prompt()
            messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

            for step in range(1, s.max_steps + 1):
                run.steps = step
                response = await self._call_model(run, step, system, tools, messages)
                # Append the whole content, not only the text: it holds the
                # tool_use blocks the results must match, and the thinking blocks.
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    return self._finish(run, response.stop_reason, _text(response))

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                # Claude may ask for several tools at once. Run them concurrently,
                # then send every result back in a single user message.
                results = await asyncio.gather(*(self._call_tool(run, step, b) for b in tool_uses))
                messages.append({"role": "user", "content": list(results)})

            # Still asking for tools after max_steps model calls: give up.
            return self._finish(run, "max_steps", "")
        except Exception as exc:
            # API errors, a lost MCP connection, bugs: close the run in the log
            # with the totals so far, so every run_start has a run_end. Then
            # re-raise and let the caller decide.
            self._finish(run, "error", "", error=f"{type(exc).__name__}: {exc}")
            raise

    async def _call_model(self, run: _Run, step: int, system: str, tools: list, messages: list):
        s = self.settings
        extra: dict[str, Any] = {}
        if s.refusal_fallback:
            # "default" lets the API pick the substitute model by refusal type.
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

        started = time.perf_counter()
        response = await self.llm.beta.messages.create(
            model=s.model,
            max_tokens=s.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            # Effort controls how much the model thinks. Thinking itself is
            # adaptive by default: the model decides when it is needed.
            output_config={"effort": s.effort},
            # Cache the request prefix (tools, system prompt, earlier turns).
            # Each step resends all of it, so later steps read it at ~5% of
            # the input price instead of paying for it again.
            cache_control={"type": "ephemeral"},
            **extra,
        )
        duration_ms = _ms_since(started)

        run.usage.add(response.usage)
        run.model = response.model  # differs from s.model if a fallback answered
        u = response.usage
        self.log.emit(
            "llm_call",
            run_id=run.run_id,
            step=step,
            model=response.model,
            stop_reason=response.stop_reason,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
            duration_ms=duration_ms,
            request_id=getattr(response, "_request_id", None),
            refusal_category=_refusal_category(response),
        )
        return response

    async def _call_tool(self, run: _Run, step: int, block) -> dict[str, Any]:
        """Run one tool_use block on the MCP server and build its tool_result."""
        started = time.perf_counter()
        try:
            result = await self.mcp.call_tool(block.name, block.input)
            text = tool_result_text(result, self.settings.max_tool_result_chars)
            is_error = bool(result.is_error)
        except MCPError as exc:
            # A protocol-level failure is still something the model can react
            # to (e.g. by trying another tool), so it goes back as a result.
            # Transport failures (server gone) are not caught: they end the run.
            text, is_error = f"Tool call failed: {exc}", True
        duration_ms = _ms_since(started)

        # Tool errors (ToolError on the server, invalid arguments, unknown
        # tool) arrive as is_error results. They are passed to the model
        # unchanged, so it can read the message and retry with a fix.
        run.tool_calls.append(ToolCall(step, block.name, block.input, duration_ms, is_error, text))
        self.log.emit(
            "tool_call",
            run_id=run.run_id,
            step=step,
            tool=block.name,
            arguments=block.input,
            duration_ms=duration_ms,
            is_error=is_error,
            error=text[:500] if is_error else None,
            result_chars=len(text),
        )
        return {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error}

    def _finish(self, run: _Run, stop_reason: str, answer: str, error: str | None = None) -> AgentResult:
        result = AgentResult(
            run_id=run.run_id,
            question=run.question,
            answer=answer,
            stop_reason=stop_reason,
            steps=run.steps,
            tool_calls=run.tool_calls,
            usage=run.usage,
            latency_ms=_ms_since(run.started),
            cost_usd=estimate_cost(run.model, run.usage),
            model=run.model,
        )
        u = run.usage
        self.log.emit(
            "run_end",
            run_id=run.run_id,
            stop_reason=stop_reason,
            steps=run.steps,
            tool_calls=len(run.tool_calls),
            tool_errors=sum(c.is_error for c in run.tool_calls),
            tools_used=[c.name for c in run.tool_calls],
            used_sql_tool=any(c.name == SQL_TOOL for c in run.tool_calls),
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
            latency_ms=result.latency_ms,
            cost_usd=result.cost_usd,
            model=run.model,
            answer=answer,
            error=error,
        )
        return result


def _text(response) -> str:
    """The text blocks of a response; thinking and tool_use blocks are skipped."""
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def _refusal_category(response) -> str | None:
    details = getattr(response, "stop_details", None)
    return getattr(details, "category", None) if response.stop_reason == "refusal" else None


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def format_summary(result: AgentResult) -> str:
    """One line for the terminal: what the question cost."""
    cost = f"~${result.cost_usd:.4f}" if result.cost_usd is not None else "cost unknown"
    tools = ", ".join(c.name for c in result.tool_calls) or "none"
    return (
        f"[{result.stop_reason}] {result.steps} steps, {len(result.tool_calls)} tool calls ({tools}), "
        f"{result.usage.input_tokens + result.usage.cache_read_input_tokens + result.usage.cache_creation_input_tokens}"
        f" in / {result.usage.output_tokens} out tokens, {result.latency_ms / 1000:.1f} s, {cost}"
    )


def to_json(result: AgentResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str)
