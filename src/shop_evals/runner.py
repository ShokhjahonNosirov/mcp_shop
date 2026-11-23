"""Run the cases through the agent, grade them, and aggregate a report.

The runner depends only on two callables: `ask` (question -> AgentResult)
and `fetch` (SQL -> rows). The CLI wires in the real agent and database;
tests pass fakes, so the whole pipeline is tested without the Claude API.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

import anthropic

from shop_agent.agent import SQL_TOOL, AgentResult
from shop_evals.cases import DOMAIN_TOOLS, Case
from shop_evals.checks import GroundTruth, grade

Ask = Callable[[str], Awaitable[AgentResult]]
Fetch = Callable[[str], list[dict[str, Any]]]

# Still failing after the SDK's own retries, but likely fine on a rerun:
# record the case as an error and carry on with the next one. Anything else
# (bad API key, unknown model, a bug) stops the whole run.
TRANSIENT_ERRORS = (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)


@dataclass
class CaseResult:
    id: str
    category: str
    question: str
    check: str
    expected_tool: str | None
    expected: Any = None
    passed: bool = False
    reason: str = ""
    # Set when the case could not be graded (ground truth or API failure).
    error: str | None = None
    answer: str = ""
    stop_reason: str | None = None
    tools_used: list[str] = field(default_factory=list)
    tool_errors: int = 0
    used_sql_tool: bool = False
    # None when no domain tool covers the question (SQL and adversarial cases).
    preferred_domain_tool: bool | None = None
    steps: int = 0
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None
    run_id: str | None = None  # links to the agent's JSONL log

    @property
    def status(self) -> str:
        return "ERROR" if self.error else "PASS" if self.passed else "FAIL"


async def compute_truth(case: Case, fetch: Fetch) -> GroundTruth:
    """Run the case's reference query (or the before-state of a refuses check)."""
    if case.check.type == "refuses":
        sql = case.check.unchanged_sql
        before = await asyncio.to_thread(fetch, sql) if sql else None
        return GroundTruth(rows=[], before=before)
    truth = GroundTruth(rows=await asyncio.to_thread(fetch, case.reference_sql))
    # Fail now, before paying for the question, if there is nothing to compare to.
    if case.check.type == "number":
        truth.number()
    else:
        truth.strings()
    return truth


async def run_case(case: Case, ask: Ask, fetch: Fetch) -> CaseResult:
    out = CaseResult(case.id, case.category, case.question, case.check.type, case.expected_tool)

    # Ground truth is computed right before the question, so "last month"
    # means the same month for the reference query and for the agent.
    try:
        truth = await compute_truth(case, fetch)
    except Exception as exc:  # noqa: BLE001 - recorded in the report; other cases still run
        out.error = f"ground truth: {type(exc).__name__}: {exc}"
        return out

    try:
        result = await ask(case.question)
    except TRANSIENT_ERRORS as exc:
        out.error = f"agent: {type(exc).__name__}: {exc}"
        return out

    if case.check.type == "refuses" and case.check.unchanged_sql:
        truth.after = await asyncio.to_thread(fetch, case.check.unchanged_sql)

    verdict = grade(case, result.answer, result.stop_reason, truth)
    tools = [c.name for c in result.tool_calls]
    out.expected = truth.expected
    out.passed, out.reason = verdict.passed, verdict.reason
    out.answer = result.answer
    out.stop_reason = result.stop_reason
    out.tools_used = tools
    out.tool_errors = sum(c.is_error for c in result.tool_calls)
    out.used_sql_tool = SQL_TOOL in tools
    if case.prefers_domain_tool:
        # Preferred = answered with the domain tools and never fell back to SQL.
        # Using no tool at all does not count: that is guessing, not preferring.
        out.preferred_domain_tool = any(t in DOMAIN_TOOLS for t in tools) and not out.used_sql_tool
    out.steps = result.steps
    out.input_tokens = result.usage.input_tokens
    out.cache_read_input_tokens = result.usage.cache_read_input_tokens
    out.cache_creation_input_tokens = result.usage.cache_creation_input_tokens
    out.output_tokens = result.usage.output_tokens
    out.latency_ms = result.latency_ms
    out.cost_usd = result.cost_usd
    out.run_id = result.run_id
    return out


async def run_evals(
    cases: list[Case],
    ask: Ask,
    fetch: Fetch,
    progress: Callable[[int, int, CaseResult], None] | None = None,
) -> list[CaseResult]:
    # One case at a time: slower than running them concurrently, but it keeps
    # latency numbers honest and stays well clear of API rate limits.
    results = []
    for i, case in enumerate(cases, start=1):
        result = await run_case(case, ask, fetch)
        results.append(result)
        if progress:
            progress(i, len(cases), result)
    return results


async def preflight(cases: list[Case], fetch: Fetch) -> list[str]:
    """Run every reference query once before any API call. Returns the problems."""
    problems = []
    for case in cases:
        try:
            await compute_truth(case, fetch)
        except Exception as exc:  # noqa: BLE001 - collected and reported together
            problems.append(f"{case.id}: {type(exc).__name__}: {exc}")
    return problems


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 2) if values else None


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    graded = [r for r in results if r.error is None]
    domain = [r for r in graded if r.preferred_domain_tool is not None]
    costs = [r.cost_usd for r in graded if r.cost_usd is not None]
    categories = sorted({r.category for r in results})
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "failed": sum(not r.passed and r.error is None for r in results),
        "errors": len(results) - len(graded),
        # Errors count as not passed: a case that could not run is no evidence.
        "pass_rate": round(sum(r.passed for r in results) / len(results), 3) if results else None,
        "by_category": {
            c: {
                "cases": sum(r.category == c for r in results),
                "passed": sum(r.passed for r in results if r.category == c),
            }
            for c in categories
        },
        # Of the questions a domain tool covers, how often the agent used one
        # and did not fall back to run_select_query.
        "domain_tool_preferred_rate": round(sum(r.preferred_domain_tool for r in domain) / len(domain), 3)
        if domain
        else None,
        "sql_tool_use_rate": round(sum(r.used_sql_tool for r in graded) / len(graded), 3) if graded else None,
        "tool_errors": sum(r.tool_errors for r in graded),
        # Averages are per graded question.
        "avg_tool_calls": _avg([len(r.tools_used) for r in graded]),
        "avg_steps": _avg([r.steps for r in graded]),
        "avg_input_tokens": _avg([r.input_tokens for r in graded]),
        "avg_cache_read_input_tokens": _avg([r.cache_read_input_tokens for r in graded]),
        "avg_output_tokens": _avg([r.output_tokens for r in graded]),
        "avg_latency_ms": _avg([r.latency_ms for r in graded]),
        "max_latency_ms": max((r.latency_ms for r in graded), default=None),
        "avg_cost_usd": round(mean(costs), 5) if costs else None,
        "total_cost_usd": round(sum(costs), 4) if costs else None,
    }


def build_report(meta: dict[str, Any], results: list[CaseResult]) -> dict[str, Any]:
    return {"meta": meta, "summary": summarize(results), "cases": [asdict(r) for r in results]}


def format_table(results: list[CaseResult]) -> str:
    """A plain-text table, one row per case."""
    header = ["case", "category", "result", "tools", "steps", "tokens in/out", "latency", "cost", "reason"]
    rows = []
    for r in results:
        tokens_in = r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
        rows.append([
            r.id,
            r.category,
            r.status,
            ",".join(r.tools_used) or "-",
            str(r.steps),
            f"{tokens_in}/{r.output_tokens}",
            f"{r.latency_ms / 1000:.1f}s",
            f"${r.cost_usd:.4f}" if r.cost_usd is not None else "-",
            _clip(r.error or r.reason, 60),
        ])
    for row in rows:
        row[3] = _clip(row[3], 45)
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=True)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in rows]
    return "\n".join(lines)


def format_summary(summary: dict[str, Any]) -> str:
    def pct(x: float | None) -> str:
        return f"{x:.0%}" if x is not None else "-"

    def num(x: float | None, fmt: str) -> str:
        return format(x, fmt) if x is not None else "-"

    by_cat = ", ".join(f"{c} {v['passed']}/{v['cases']}" for c, v in summary["by_category"].items())
    latency_ms = summary["avg_latency_ms"]
    latency_s = latency_ms / 1000 if latency_ms is not None else None
    passed = (
        f"Passed {summary['passed']}/{summary['cases']} ({pct(summary['pass_rate'])}); "
        f"{summary['failed']} failed, {summary['errors']} errors. By category: {by_cat}"
    )
    tools = (
        f"Domain tool preferred over SQL: {pct(summary['domain_tool_preferred_rate'])}; "
        f"run_select_query used in {pct(summary['sql_tool_use_rate'])} of questions; "
        f"{summary['tool_errors']} tool errors"
    )
    per_question = (
        f"Per question: {num(summary['avg_tool_calls'], '.1f')} tool calls, "
        f"{num(summary['avg_input_tokens'], '.0f')} uncached + "
        f"{num(summary['avg_cache_read_input_tokens'], '.0f')} cached input / "
        f"{num(summary['avg_output_tokens'], '.0f')} output tokens, {num(latency_s, '.1f')} s, "
        f"${num(summary['avg_cost_usd'], '.4f')}. Total ${num(summary['total_cost_usd'], '.4f')}"
    )
    return f"{passed}\n{tools}\n{per_question}"


def _clip(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."
