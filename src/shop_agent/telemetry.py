"""Structured run logs: one JSON object per line (JSON Lines).

Every event carries the run id, so one file can hold many runs and still be
grouped per question afterwards, e.g. with pandas, DuckDB or jq. Events:

- ``run_start``: question, model and limits
- ``llm_call``: one Messages API request: tokens, latency, stop reason
- ``tool_call``: one MCP tool call: name, arguments, latency, error
- ``run_end``: totals for the question: tokens, tool calls, latency, cost

Questions and answers are logged in full, which suits a demo with invented
data. With real users they may hold personal data and need redacting.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO


class EventLog:
    def __init__(self, path: str | Path | None = None, *, echo: TextIO | None = None) -> None:
        """Write events to a JSONL file, and optionally a short trace to `echo`."""
        self.path = Path(path) if path else None
        self.echo = echo
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, **fields}
        if self.path:
            # Opened per event so each line is flushed, and a crash mid-run
            # still leaves every event up to that point on disk.
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.echo:
            self._print_trace(record)
        return record

    def _print_trace(self, r: dict[str, Any]) -> None:
        if r["event"] == "tool_call":
            status = "ERROR " if r["is_error"] else ""
            args = json.dumps(r["arguments"], ensure_ascii=False)
            print(f"  -> {status}{r['tool']}({args}) {r['duration_ms']} ms", file=self.echo)
        elif r["event"] == "llm_call":
            print(
                f"  [step {r['step']}] {r['stop_reason']}, "
                f"{r['input_tokens']} in / {r['output_tokens']} out tokens, {r['duration_ms']} ms",
                file=self.echo,
            )


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL log back into a list of dicts."""
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
