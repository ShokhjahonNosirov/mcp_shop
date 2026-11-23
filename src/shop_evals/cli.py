"""Command line: ``uv run shop-evals``.

Needs the database (for the ground truth) and a Claude API key (for the
agent). Every case is a real, billed agent run, so use --only or
--category to try a few cases first.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
from mcp import Client
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from shop_agent.agent import ShopAgent
from shop_agent.cli import explain_error, make_llm, mcp_server
from shop_agent.config import AgentSettings
from shop_agent.telemetry import EventLog
from shop_evals.cases import DEFAULT_CASES, Case, load_cases
from shop_evals.runner import (
    CaseResult,
    Fetch,
    build_report,
    compute_truth,
    format_summary,
    format_table,
    preflight,
    run_evals,
)
from shop_mcp.config import Settings as DatabaseSettings
from shop_mcp.db import Database

# Newest order older than this: the database was seeded a while ago, so
# "last 30 days" style questions may match little or nothing.
STALE_AFTER = timedelta(days=3)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="shop-evals", description="Run the shop-agent eval cases and write a JSON report.")
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES, help=f"case file (default: {DEFAULT_CASES})")
    p.add_argument("--only", help="comma-separated case ids")
    p.add_argument("--category", help="only cases in this category")
    p.add_argument("--output", type=Path, help="report path (default: evals/reports/<timestamp>.json)")
    p.add_argument("--min-pass-rate", type=float, default=0.0, help="exit 1 below this pass rate (0-1)")
    p.add_argument("--list", action="store_true", help="list the selected cases and exit (no API calls)")
    p.add_argument("--dry-run", action="store_true", help="print the ground truth only (needs the DB, no API calls)")
    p.add_argument("--transport", choices=["stdio", "http"])
    p.add_argument("--url", dest="mcp_url")
    p.add_argument("--model")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-steps", type=int)
    return p.parse_args(argv)


def select_cases(cases: list[Case], only: str | None, category: str | None) -> list[Case]:
    if only:
        wanted = {i.strip() for i in only.split(",") if i.strip()}
        unknown = wanted - {c.id for c in cases}
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in wanted]
    if category:
        cases = [c for c in cases if c.category == category]
    if not cases:
        raise ValueError("no cases selected")
    return cases


def git_commit() -> str | None:
    """The commit being evaluated, so reports from different runs can be compared."""
    if sha := os.environ.get("GITHUB_SHA"):
        return sha[:12]
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def print_progress(i: int, total: int, r: CaseResult) -> None:
    detail = r.error or r.reason
    print(f"[{i:>2}/{total}] {r.status:<5} {r.id:<32} {detail[:90]}", file=sys.stderr, flush=True)


async def run(cases: list[Case], settings: AgentSettings, fetch: Fetch, log_path: Path) -> list[CaseResult]:
    # One Claude client and one MCP connection for the whole run; the agent
    # lists the tools once and reuses them for every question.
    log = EventLog(log_path)
    async with make_llm(settings) as llm, Client(mcp_server(settings), read_timeout_seconds=30) as mcp:
        agent = ShopAgent(llm, mcp, settings, log)
        return await run_evals(cases, agent.run, fetch, progress=print_progress)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        cases = select_cases(load_cases(args.cases), args.only, args.category)
    except (OSError, ValueError, ValidationError) as exc:
        sys.exit(f"Cannot load cases from {args.cases}: {exc}")

    if args.list:
        for c in cases:
            print(f"{c.id:<32} {c.category:<12} {c.check.type:<9} {c.question}")
        return

    started = datetime.now(UTC)
    output = args.output or Path("evals/reports") / f"{started:%Y%m%d-%H%M%S}.json"
    # The agent's own JSONL events go next to the report, linked by run_id.
    log_path = output.with_suffix(".jsonl")
    overrides = {
        k: v for k in ("transport", "mcp_url", "model", "effort", "max_steps") if (v := getattr(args, k)) is not None
    }
    try:
        settings = AgentSettings(**overrides, log_file=str(log_path))
        # Ground truth is read through the server's own read-only Database
        # class, with the same SHOP_MCP_DATABASE_URL as the server.
        db = Database(DatabaseSettings())
        db.open()
    except ValidationError as exc:
        sys.exit(f"Invalid settings (is SHOP_MCP_DATABASE_URL set?):\n{exc}")
    except (PoolTimeout, psycopg.Error) as exc:
        sys.exit(f"Cannot connect to the database for ground truth: {exc}. Is it running? (docker compose up -d db)")

    def fetch(sql: str) -> list[dict[str, Any]]:
        # cast: reference SQL comes from the repo's case file, not from a user.
        return db.fetch(cast(LiteralString, sql)).rows

    try:
        # Check every reference query before spending anything on the API.
        problems = asyncio.run(preflight(cases, fetch))
        if problems:
            sys.exit("Ground truth failed for some cases (no API calls made):\n  " + "\n  ".join(problems))
        if args.dry_run:
            for case in cases:
                truth = asyncio.run(compute_truth(case, fetch))
                print(f"{case.id:<32} {case.check.type:<9} {truth.expected}")
            return
        latest = fetch("SELECT max(ordered_at) AS latest FROM orders")[0]["latest"]
        if latest and started - latest > STALE_AFTER:
            print(
                f"Warning: the newest order is from {latest:%Y-%m-%d}. Recent-period questions see little data; "
                "reset with `docker compose down -v && docker compose up -d --wait db`.",
                file=sys.stderr,
            )

        print(f"Running {len(cases)} cases with {settings.model} (effort {settings.effort})", file=sys.stderr)
        try:
            results = asyncio.run(run(cases, settings, fetch, log_path))
        except Exception as exc:
            message = explain_error(exc, settings)
            if message is None:
                raise
            sys.exit(message)
    finally:
        db.close()

    finished = datetime.now(UTC)
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "model": settings.model,
        "effort": settings.effort,
        "max_steps": settings.max_steps,
        "transport": settings.transport,
        "cases_file": str(args.cases),
        "git_commit": git_commit(),
        "data_as_of": latest.isoformat() if latest else None,
        "agent_log": str(log_path),
    }
    report = build_report(meta, results)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(format_table(results))
    print()
    print(format_summary(report["summary"]))
    print(f"Report: {output}")
    sys.exit(0 if report["summary"]["pass_rate"] >= args.min_pass_rate else 1)
