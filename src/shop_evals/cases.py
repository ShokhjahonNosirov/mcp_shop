"""The eval case file (evals/cases.yaml), parsed and validated.

Validation runs before any API call, so a typo in the YAML fails in a
second instead of halfway through a paid run.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CASES = Path("evals/cases.yaml")

# The tools that encode business rules. An agent that answers a question
# these cover with hand-written SQL may get it right, but it is taking the
# riskier path (it can miss rules such as which order statuses count).
DOMAIN_TOOLS = {"find_customers", "get_customer_orders", "top_products", "revenue_summary"}


class _Strict(BaseModel):
    # A misspelt key (e.g. "tolerence") is an error, not silently ignored.
    model_config = ConfigDict(extra="forbid")


class NumberCheck(_Strict):
    type: Literal["number"]
    # Relative tolerance: 0.005 accepts 0.5% off, so "€72,953" passes for
    # 72,953.13. 0 means the number must match exactly (counts).
    tolerance: float = Field(default=0.005, ge=0, le=0.5)


class ContainsCheck(_Strict):
    type: Literal["contains"]
    match: Literal["all", "any"] = "all"


class RefusesCheck(_Strict):
    type: Literal["refuses"]
    # Strings that must never appear in the answer, e.g. credentials.
    forbidden: list[str] = []
    # A query whose result must be the same before and after the run.
    unchanged_sql: str | None = None


Check = Annotated[NumberCheck | ContainsCheck | RefusesCheck, Field(discriminator="type")]


class Case(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    category: str
    question: str
    expected_tool: str | None
    check: Check
    reference_sql: str | None = None

    @model_validator(mode="after")
    def _reference_sql_when_needed(self) -> "Case":
        needs_sql = self.check.type in ("number", "contains")
        if needs_sql and not self.reference_sql:
            raise ValueError(f"case {self.id!r}: a {self.check.type} check needs reference_sql")
        if not needs_sql and self.reference_sql:
            raise ValueError(f"case {self.id!r}: reference_sql is unused by a refuses check (use unchanged_sql)")
        return self

    @property
    def prefers_domain_tool(self) -> bool:
        """True when a domain tool can answer this, so SQL should not be needed."""
        return self.expected_tool in DOMAIN_TOOLS


class CaseFile(_Strict):
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "CaseFile":
        ids = [c.id for c in self.cases]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
        return self


def load_cases(path: str | Path = DEFAULT_CASES) -> list[Case]:
    with Path(path).open(encoding="utf-8") as f:
        return CaseFile.model_validate(yaml.safe_load(f)).cases
