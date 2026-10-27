"""Unit tests for the SQL guard. No database needed."""

import pytest

from shop_mcp.sql_guard import MAX_QUERY_LENGTH, UnsafeQueryError, validate_select


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "select id, email from customers where country = 'PL' order by id limit 5",
        "SELECT category, sum(price) FROM products GROUP BY category HAVING sum(price) > 100",
        "WITH recent AS (SELECT * FROM orders WHERE ordered_at > now() - interval '30 days') SELECT count(*) FROM recent",
        "SELECT id FROM customers UNION SELECT customer_id FROM orders",
        "SELECT c.full_name, count(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY 1",
        "SELECT count(*) FILTER (WHERE status = 'cancelled') FROM orders",
        "SELECT * FROM (SELECT id FROM products) AS sub",
        "SELECT name FROM products WHERE name ILIKE '%mug%'",
    ],
)
def test_accepts_read_only_queries(query):
    assert validate_select(query) == query


def test_strips_whitespace_and_trailing_semicolons():
    assert validate_select("  SELECT 1;;  \n") == "SELECT 1"


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO customers (email) VALUES ('x@example.com')",
        "UPDATE products SET price = 0",
        "DELETE FROM orders",
        "MERGE INTO products p USING products q ON p.id = q.id WHEN MATCHED THEN DELETE",
        "DROP TABLE orders",
        "TRUNCATE orders",
        "CREATE TABLE x (id int)",
        "ALTER TABLE products ADD COLUMN x int",
        "GRANT ALL ON orders TO PUBLIC",
        "COPY orders TO STDOUT",
        "SET search_path = public",
        "BEGIN",
        "VACUUM orders",
        "EXPLAIN ANALYZE DELETE FROM orders",
        "DO $$ BEGIN DELETE FROM orders; END $$",
        "CALL some_procedure()",
    ],
)
def test_rejects_statements_that_are_not_selects(query):
    with pytest.raises(UnsafeQueryError):
        validate_select(query)


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("SELECT 1; DROP TABLE orders", "exactly one statement"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone", "DELETE"),
        ("WITH x AS (UPDATE products SET price = 0 RETURNING id) SELECT * FROM x", "UPDATE"),
        ("SELECT * INTO backup FROM orders", "INTO"),
        ("SELECT * FROM orders FOR UPDATE", "row locking"),
        ("SELECT * FROM orders FOR SHARE", "row locking"),
    ],
)
def test_rejects_writes_hidden_inside_selects(query, reason):
    with pytest.raises(UnsafeQueryError, match=reason):
        validate_select(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_sleep(60)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_terminate_backend(123)",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT nextval('orders_id_seq')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT query_to_xml('DELETE FROM orders', true, true, '')",
        "SELECT dblink_exec('host=evil', 'DROP TABLE x')",
        "SELECT id FROM orders WHERE id IN (SELECT PG_SLEEP(1))",
    ],
)
def test_rejects_dangerous_functions(query):
    with pytest.raises(UnsafeQueryError, match="not allowed"):
        validate_select(query)


@pytest.mark.parametrize("query", ["", "   ", ";"])
def test_rejects_empty_input(query):
    with pytest.raises(UnsafeQueryError, match="empty"):
        validate_select(query)


def test_rejects_overlong_input():
    with pytest.raises(UnsafeQueryError, match="longer than"):
        validate_select("SELECT " + "1 + " * MAX_QUERY_LENGTH + "1")


def test_rejects_unparseable_input():
    with pytest.raises(UnsafeQueryError):
        validate_select("SELEC id FROM")
