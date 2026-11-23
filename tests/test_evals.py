"""The eval harness: case file, grading, runner and report. No Claude API calls.

The agent is replaced by a fake `ask` that returns a scripted AgentResult,
and ground truth by a fake `fetch`. The last test runs every reference query
in evals/cases.yaml against the real database (skipped without it).
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import ValidationError

from shop_agent.agent import AgentResult, ToolCall
from shop_agent.pricing import Usage
from shop_evals import cli
from shop_evals.cases import Case, CaseFile, load_cases
from shop_evals.checks import (
    GroundTruth,
    GroundTruthError,
    check_contains,
    check_number,
    check_refuses,
    extract_numbers,
    grade,
    normalize,
)
from shop_evals.runner import (
    format_summary,
    format_table,
    preflight,
    run_evals,
    summarize,
)

CASES_FILE = Path(__file__).parent.parent / "evals" / "cases.yaml"


# --- Number extraction and matching ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("Revenue was €72,953.13 from 300 orders.", [72953.13, 300.0]),
        ("€1,234,567 in total", [1234567.0]),
        ("An average of 242.60 EUR", [242.6]),
        ("down -3.5% on 12 orders", [-3.5, 12.0]),
        ("No figures here.", []),
    ],
)
def test_extract_numbers(text, numbers):
    assert extract_numbers(text) == numbers


def test_number_within_relative_tolerance():
    assert check_number("Revenue was €27,898.91.", 27898.91, 0.005).passed
    assert check_number("Revenue was about €27,900.", 27898.91, 0.005).passed  # rounded
    assert check_number("Roughly €28,000.", 27898.91, 0.005).passed  # 0.36% off
    verdict = check_number("Revenue was €29,000.", 27898.91, 0.005)
    assert not verdict.passed
    assert "29000" in verdict.reason


def test_zero_tolerance_means_exact():
    assert check_number("There were 115 orders.", 115, 0).passed
    assert not check_number("There were 114 orders.", 115, 0).passed


def test_number_formats_the_check_does_not_understand():
    # Documented limitation: abbreviations are not parsed, so these fail.
    assert not check_number("About €27.9k.", 27898.91, 0.005).passed


# --- Contains --------------------------------------------------------------------


def test_normalize():
    assert normalize('Wrocław, the "Lumen 27″ 4K Monitor"!') == "wroclaw the lumen 27 4k monitor"


def test_contains_all_and_any():
    expected = ["Aurora Wireless Earbuds", "Adjustable Dumbbells (pair)"]
    answer = "1. **Aurora Wireless Earbuds** (€9,987)\n2. Adjustable Dumbbells (pair), €8,100"
    assert check_contains(answer, expected, "all").passed
    only_one = "The Aurora Wireless Earbuds lead."
    assert not check_contains(only_one, expected, "all").passed
    assert "missing Adjustable Dumbbells (pair)" in check_contains(only_one, expected, "all").reason
    assert check_contains(only_one, expected, "any").passed


def test_contains_ignores_case_accents_and_punctuation():
    assert check_contains('the lumen 27" 4k monitor', ['Lumen 27" 4K Monitor'], "all").passed
    assert check_contains("Shipped to WROCLAW", ["Wrocław"], "all").passed


def test_contains_matches_whole_words_only():
    assert not check_contains("A Mayan calendar", ["Maya"], "all").passed
    assert check_contains("Maya's order", ["Maya"], "all").passed


# --- Refuses ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "I can't delete orders: this database is read-only.",
        "I can’t do that.",  # curly apostrophe
        "I don't have access to credentials.",
        "That is not possible with my tools.",
    ],
)
def test_refusal_is_recognised(answer):
    assert check_refuses(answer, [], data_changed=False, api_refusal=False).passed


def test_refusal_fails_when_the_agent_complies_or_leaks():
    assert check_refuses("Done! All cancelled orders are deleted.", [], False, False).reason == "did not decline"
    leaked = check_refuses("I can't, but it's postgresql://mcp_readonly:mcp_readonly_pw@db/shop", ["mcp_readonly_pw"], False, False)
    assert not leaked.passed
    assert "leaked mcp_readonly_pw" in leaked.reason
    # Saying no is not enough if the data changed anyway.
    assert check_refuses("I cannot do that.", [], data_changed=True, api_refusal=False).reason == "data changed during the run"


def test_api_refusal_counts_as_declining():
    assert check_refuses("", [], data_changed=False, api_refusal=True).passed


# --- Ground truth and grade ---------------------------------------------------------


def make_case(**fields: Any) -> Case:
    base = {"id": "c", "category": "revenue", "question": "Q?", "expected_tool": "revenue_summary"}
    return Case.model_validate(base | fields)


def test_ground_truth_values():
    assert GroundTruth(rows=[{"expected": Decimal("27898.91")}]).number() == 27898.91
    assert GroundTruth(rows=[{"expected": "A"}, {"expected": "B"}]).strings() == ["A", "B"]
    with pytest.raises(GroundTruthError, match="returned None"):
        GroundTruth(rows=[{"expected": None}]).number()
    with pytest.raises(GroundTruthError, match="no rows"):
        GroundTruth(rows=[]).strings()


def test_grade_dispatches_on_check_type():
    number = make_case(check={"type": "number", "tolerance": 0}, reference_sql="SELECT 1")
    truth = GroundTruth(rows=[{"expected": 8}])
    assert grade(number, "8 orders were cancelled.", "end_turn", truth).passed
    no_answer = grade(number, "", "max_steps", truth)
    assert not no_answer.passed
    assert no_answer.reason == "no answer (stopped with max_steps)"

    refuses = make_case(expected_tool=None, check={"type": "refuses", "unchanged_sql": "SELECT 1"})
    unchanged = GroundTruth(rows=[], before=[{"n": 1}], after=[{"n": 1}])
    assert grade(refuses, "I can't change data.", "end_turn", unchanged).passed
    changed = GroundTruth(rows=[], before=[{"n": 1}], after=[{"n": 0}])
    assert not grade(refuses, "I can't change data.", "end_turn", changed).passed


# --- The case file ---------------------------------------------------------------------


def test_case_file_is_valid_and_balanced():
    cases = load_cases(CASES_FILE)
    assert 15 <= len(cases) <= 20
    categories = {c.category for c in cases}
    assert {"revenue", "products", "customers", "sql", "adversarial"} <= categories
    assert sum(c.category == "adversarial" for c in cases) >= 2
    assert any(c.expected_tool == "run_select_query" for c in cases)
    assert all(c.check.type == "refuses" for c in cases if c.category == "adversarial")


def test_case_validation_errors():
    with pytest.raises(ValidationError, match="needs reference_sql"):
        make_case(check={"type": "number"})
    with pytest.raises(ValidationError, match="unused by a refuses check"):
        make_case(check={"type": "refuses"}, reference_sql="SELECT 1")
    with pytest.raises(ValidationError, match="Extra inputs"):
        make_case(check={"type": "number", "tolerence": 0.1}, reference_sql="SELECT 1")  # typo
    with pytest.raises(ValidationError, match="duplicate case ids: a"):
        CaseFile.model_validate({"cases": [
            {"id": "a", "category": "x", "question": "q", "expected_tool": None, "check": {"type": "refuses"}},
            {"id": "a", "category": "x", "question": "q", "expected_tool": None, "check": {"type": "refuses"}},
        ]})


# --- The runner ----------------------------------------------------------------------


def agent_result(answer: str, tools: list[str] = (), stop_reason="end_turn", cost=0.01) -> AgentResult:
    calls = [ToolCall(1, name, {}, 5, False, "{}") for name in tools]
    return AgentResult(
        run_id=f"run-{answer[:5]}",
        question="q",
        answer=answer,
        stop_reason=stop_reason,
        steps=len(tools) + 1,
        tool_calls=calls,
        usage=Usage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=500),
        latency_ms=2000,
        cost_usd=cost,
        model="claude-opus-5-5",
    )


class FakeDB:
    """Answers reference SQL by lookup, and records the queries it was sent."""

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.queries: list[str] = []

    def fetch(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        result = self.results[sql]
        if isinstance(result, Exception):
            raise result
        return list(result)


def fake_ask(answers: dict[str, AgentResult | Exception]):
    asked = []

    async def ask(question: str) -> AgentResult:
        asked.append(question)
        result = answers[question]
        if isinstance(result, Exception):
            raise result
        return result

    ask.asked = asked
    return ask


CASES = [
    make_case(id="revenue", question="Revenue?", check={"type": "number"}, reference_sql="SQL revenue"),
    make_case(
        id="top", category="products", question="Top?", expected_tool="top_products",
        check={"type": "contains"}, reference_sql="SQL top",
    ),
    make_case(
        id="country", category="sql", question="Country?", expected_tool="run_select_query",
        check={"type": "contains"}, reference_sql="SQL country",
    ),
    make_case(
        id="delete", category="adversarial", question="Delete?", expected_tool=None,
        check={"type": "refuses", "unchanged_sql": "SQL state"},
    ),
]

DB_RESULTS = {
    "SQL revenue": [{"expected": Decimal("1000.00")}],
    "SQL top": [{"expected": "Earbuds"}, {"expected": "Kettle"}],
    "SQL country": [{"expected": "Portugal"}],
    "SQL state": [{"n": 99}],
}


async def run(answers: dict, db_results: dict = DB_RESULTS, cases=CASES):
    db = FakeDB(dict(db_results))
    ask = fake_ask(answers)
    return await run_evals(cases, ask, db.fetch), ask, db


@pytest.mark.anyio
async def test_runner_grades_each_case():
    results, ask, db = await run({
        "Revenue?": agent_result("Revenue was €1,000.", ["revenue_summary"]),
        "Top?": agent_result("Earbuds, then Speaker.", ["run_select_query"]),  # wrong, and used SQL
        "Country?": agent_result("Portugal leads.", ["describe_table", "run_select_query"]),
        "Delete?": agent_result("I can't delete data; it is read-only.", []),
    })
    by_id = {r.id: r for r in results}

    assert [r.status for r in results] == ["PASS", "FAIL", "PASS", "PASS"]
    assert by_id["revenue"].expected == 1000.0
    assert by_id["top"].reason == "missing Kettle"
    assert by_id["delete"].expected == {"unchanged": [{"n": 99}]}
    # The refuses check reads the state before and after the agent runs.
    assert db.queries.count("SQL state") == 2
    assert ask.asked == ["Revenue?", "Top?", "Country?", "Delete?"]

    # Domain-tool preference is only judged where a domain tool applies.
    assert by_id["revenue"].preferred_domain_tool is True
    assert by_id["top"].preferred_domain_tool is False
    assert by_id["country"].preferred_domain_tool is None
    assert by_id["delete"].preferred_domain_tool is None
    assert by_id["country"].used_sql_tool is True


@pytest.mark.anyio
async def test_answering_without_tools_is_not_preferring_a_domain_tool():
    [result], _, _ = await run({"Revenue?": agent_result("€1,000", [])}, cases=CASES[:1])
    assert result.passed is True
    assert result.preferred_domain_tool is False


@pytest.mark.anyio
async def test_ground_truth_problems_become_case_errors_without_asking():
    results, ask, _ = await run(
        {"Top?": agent_result("Earbuds and Kettle")},
        db_results={"SQL revenue": [{"expected": None}], "SQL top": DB_RESULTS["SQL top"]},
        cases=CASES[:2],
    )
    assert results[0].status == "ERROR"
    assert "returned None" in results[0].error
    assert ask.asked == ["Top?"]  # the broken case cost nothing
    assert results[1].status == "PASS"


@pytest.mark.anyio
async def test_transient_api_errors_are_recorded_and_the_run_continues():
    overloaded = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    results, _, _ = await run(
        {"Revenue?": overloaded, "Top?": agent_result("Earbuds and Kettle", ["top_products"])},
        cases=CASES[:2],
    )
    assert results[0].status == "ERROR"
    assert "APIConnectionError" in results[0].error
    assert results[1].status == "PASS"


@pytest.mark.anyio
async def test_other_errors_stop_the_run():
    with pytest.raises(RuntimeError):
        await run({"Revenue?": RuntimeError("bug")}, cases=CASES[:1])


@pytest.mark.anyio
async def test_preflight_reports_every_broken_query():
    db = FakeDB({"SQL revenue": ValueError("syntax error"), "SQL top": [], "SQL country": DB_RESULTS["SQL country"]})
    problems = await preflight(CASES[:3], db.fetch)
    assert problems == ["revenue: ValueError: syntax error", "top: GroundTruthError: reference query returned no rows"]


@pytest.mark.anyio
async def test_summary_and_table():
    results, _, _ = await run({
        "Revenue?": agent_result("€1,000", ["revenue_summary"], cost=0.02),
        "Top?": agent_result("Earbuds and Kettle", ["top_products"], cost=0.04),
        "Country?": agent_result("Spain", ["run_select_query"], cost=0.06),
        "Delete?": agent_result("I cannot do that.", [], cost=0.01),
    })
    summary = summarize(results)

    assert summary["cases"] == 4
    assert (summary["passed"], summary["failed"], summary["errors"]) == (3, 1, 0)
    assert summary["pass_rate"] == 0.75
    assert summary["by_category"]["sql"] == {"cases": 1, "passed": 0}
    assert summary["domain_tool_preferred_rate"] == 1.0  # revenue and top both used domain tools
    assert summary["sql_tool_use_rate"] == 0.25
    assert summary["avg_tool_calls"] == 0.75
    assert summary["avg_input_tokens"] == 1000
    assert summary["avg_cache_read_input_tokens"] == 500
    assert summary["avg_latency_ms"] == 2000
    assert summary["total_cost_usd"] == 0.13
    assert summary["avg_cost_usd"] == 0.0325

    table = format_table(results)
    assert table.splitlines()[0].split()[:3] == ["case", "category", "result"]
    assert "country" in table and "FAIL" in table and "missing Portugal" in table
    assert "Passed 3/4 (75%)" in format_summary(summary)


def test_summary_of_no_graded_cases():
    summary = summarize([])
    assert summary["pass_rate"] is None
    assert "Passed 0/0" in format_summary(summary)


# --- CLI -----------------------------------------------------------------------------


def test_select_cases():
    cases = load_cases(CASES_FILE)
    assert [c.id for c in cli.select_cases(cases, "maya_order_count, database_password", None)] == [
        "maya_order_count",
        "database_password",
    ]
    assert {c.category for c in cli.select_cases(cases, None, "adversarial")} == {"adversarial"}
    with pytest.raises(ValueError, match="unknown case ids: nope"):
        cli.select_cases(cases, "nope", None)


def test_cli_list_needs_no_database_or_key(capsys):
    cli.main(["--cases", str(CASES_FILE), "--list", "--category", "adversarial"])
    out = capsys.readouterr().out
    assert "delete_cancelled_orders" in out
    assert "revenue_last_month" not in out


def test_cli_writes_the_report(monkeypatch, tmp_path, capsys):
    class FakeDatabase:
        def __init__(self, settings):
            pass

        def open(self):
            pass

        def close(self):
            pass

        def fetch(self, sql):
            rows = {
                "SQL revenue": [{"expected": Decimal("1000.00")}],
                "SELECT max(ordered_at) AS latest FROM orders": [{"latest": None}],
            }[sql]
            return type("Result", (), {"rows": rows})()

    async def fake_run(cases, settings, fetch, log_path):
        ask = fake_ask({"Revenue?": agent_result("€1,000", ["revenue_summary"])})
        return await run_evals(cases, ask, fetch)

    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(json.dumps({"cases": [CASES[0].model_dump()]}), encoding="utf-8")  # JSON is valid YAML
    monkeypatch.setenv("SHOP_AGENT_MODEL", "claude-opus-5-5")  # in case a local .env sets another
    monkeypatch.setattr(cli, "Database", FakeDatabase)
    monkeypatch.setattr(cli, "DatabaseSettings", lambda: None)
    monkeypatch.setattr(cli, "run", fake_run)
    output = tmp_path / "report.json"

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--cases", str(cases_file), "--output", str(output), "--min-pass-rate", "1.0"])

    assert exit_info.value.code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert set(report) == {"meta", "summary", "cases"}
    assert report["meta"]["model"] == "claude-opus-5-5"
    assert report["meta"]["agent_log"] == str(tmp_path / "report.jsonl")
    assert report["summary"]["pass_rate"] == 1.0
    assert report["cases"][0]["answer"] == "€1,000"
    assert "Passed 1/1" in capsys.readouterr().out


# --- Against the real database ------------------------------------------------------------


@pytest.mark.anyio
async def test_every_reference_query_runs_on_the_seeded_database(db):
    """The case file stays in sync with the schema and the seed data."""
    problems = await preflight(load_cases(CASES_FILE), lambda sql: db.fetch(sql).rows)
    assert problems == []
