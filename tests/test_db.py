"""Tests for the query runner: results, limits and error messages.

Read-only enforcement has its own file, test_readonly_enforcement.py.
"""

import pytest

from shop_mcp.db import QueryError


def test_returns_columns_and_rows(db):
    result = db.fetch("SELECT id, email FROM shop.customers ORDER BY id", max_rows=3)
    assert result.columns == ["id", "email"]
    assert [row["id"] for row in result.rows] == [1, 2, 3]


def test_binds_parameters(db):
    result = db.fetch("SELECT id FROM shop.customers WHERE id = %(id)s", {"id": 7})
    assert result.rows == [{"id": 7}]


def test_marks_truncated_results(db):
    result = db.fetch("SELECT id FROM shop.orders", max_rows=5)
    assert len(result.rows) == 5
    assert result.truncated is True


def test_not_truncated_when_everything_fits(db):
    result = db.fetch("SELECT id FROM shop.products WHERE category = 'Books'", max_rows=10)
    assert len(result.rows) == 6
    assert result.truncated is False


def test_caller_cannot_exceed_configured_max_rows(db):
    # db fixture is configured with max_rows=50.
    result = db.fetch("SELECT id FROM shop.orders", max_rows=10_000)
    assert len(result.rows) == 50


def test_statement_timeout_cancels_slow_queries(db):
    with pytest.raises(QueryError, match="statement timeout"):
        db.fetch("SELECT pg_sleep(2)")


def test_bad_sql_returns_postgres_message(db):
    with pytest.raises(QueryError, match='column "nope" does not exist'):
        db.fetch("SELECT nope FROM shop.customers")
