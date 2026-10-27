"""Proof that nothing can be written through this server.

Read-only is enforced by several independent layers. Each section below
attacks one layer on its own, bypassing the layers in front of it, so a
regression in any single layer fails a test even while the others hold.

    1. SQL guard        sql_guard.validate_select (unit tests: test_sql_guard.py)
    2. Query runner     server-side cursor + extended protocol (db.py)
    3. Transaction      every transaction is READ ONLY (db.py)
    4. Database role    mcp_readonly has SELECT grants only (03_readonly_role.sh)

The last section goes through the real MCP tool, end to end, and checks that
the data is byte-for-byte unchanged afterwards.
"""

import psycopg
import pytest
from psycopg import errors

from shop_mcp.db import Database, QueryError

from .conftest import TEST_DATABASE_URL, make_settings

INSERT_CUSTOMER = (
    "INSERT INTO shop.customers (email, full_name, country, city) VALUES ('x@example.com', 'X', 'PL', 'Lodz')"
)

# --- Layer 2: query runner (guard bypassed: db.fetch called directly) -------


@pytest.mark.parametrize(
    "statement",
    [
        INSERT_CUSTOMER,
        "UPDATE shop.products SET price = 0.01",
        "DELETE FROM shop.orders",
        "DROP TABLE shop.order_items",
        # A data-modifying CTE looks like a SELECT from the outside.
        "WITH gone AS (DELETE FROM shop.orders RETURNING id) SELECT * FROM gone",
        # Extended query protocol: one statement per execute, no stacking.
        "SELECT 1; DROP TABLE shop.orders",
    ],
)
def test_runner_only_executes_a_single_query(db, statement):
    """DECLARE ... CURSOR accepts only a plain query, one statement at a time."""
    with pytest.raises(
        QueryError,
        match="syntax error|DECLARE CURSOR must not contain|cannot insert multiple commands",
    ):
        db.fetch(statement)


# --- Layer 3: read-only transaction (guard and runner shape bypassed) -------


@pytest.mark.parametrize(
    "query",
    [
        # lo_create is executable by every role, so only the transaction mode stops it.
        "SELECT lo_create(0)",
        "SELECT * FROM shop.orders FOR UPDATE",
    ],
)
def test_read_only_transaction_blocks_side_effects_inside_selects(db, query):
    with pytest.raises(QueryError, match="in a read-only transaction"):
        db.fetch(query)


def test_transaction_cannot_be_switched_to_read_write(db):
    with pytest.raises(QueryError, match="read-write mode must be set before any query"):
        db.fetch("SELECT set_config('transaction_read_only', 'off', true)")


def test_session_changes_do_not_leak_to_the_next_query(database_available):
    """Every transaction is rolled back, and session settings roll back with it."""
    database = Database(make_settings(pool_min_size=1, pool_max_size=1))  # same connection twice
    database.open()
    try:
        database.fetch("SELECT set_config('search_path', 'public', false)")
        database.fetch("SELECT set_config('default_transaction_read_only', 'off', false)")
        # search_path is back to the role default (shop), so this resolves...
        assert database.fetch("SELECT count(*) AS n FROM orders").rows == [{"n": 1500}]
        # ...and the session default is read-only again.
        setting = database.fetch("SELECT current_setting('default_transaction_read_only') AS v")
        assert setting.rows == [{"v": "on"}]
    finally:
        database.close()


# --- Layer 4: database role (application bypassed entirely) -----------------


@pytest.fixture
def raw_conn(database_available):
    """A plain connection as mcp_readonly, explicitly in READ WRITE mode."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.execute("BEGIN READ WRITE")
        yield conn


@pytest.mark.parametrize(
    "statement",
    [
        INSERT_CUSTOMER,
        "UPDATE shop.products SET price = 0.01",
        "DELETE FROM shop.orders",
        "TRUNCATE shop.order_items",
        "SELECT nextval('shop.orders_id_seq')",
    ],
)
def test_role_has_no_write_privileges(raw_conn, statement):
    with pytest.raises(errors.InsufficientPrivilege):
        raw_conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE shop.orders",
        "ALTER TABLE shop.products ADD COLUMN hacked int",
        "CREATE TABLE shop.stolen (x int)",
        "CREATE TABLE public.stolen (x int)",
        "CREATE TEMP TABLE stolen (x int)",
        "SELECT pg_read_file('/etc/passwd')",
    ],
)
def test_role_cannot_change_schema_or_read_server_files(raw_conn, statement):
    with pytest.raises(errors.InsufficientPrivilege):
        raw_conn.execute(statement)


def test_role_is_read_only_by_default(raw_conn):
    """Even a client that forgets READ ONLY gets it from the role settings."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SHOW statement_timeout").fetchone() == ("10s",)


# --- End to end: the MCP tool --------------------------------------------------

ATTACKS = [
    INSERT_CUSTOMER,
    "UPDATE shop.products SET price = 0.01",
    "DELETE FROM shop.orders",
    "DROP TABLE shop.order_items",
    "TRUNCATE shop.orders",
    "WITH gone AS (DELETE FROM shop.orders RETURNING id) SELECT * FROM gone",
    "SELECT 1; DELETE FROM shop.orders",
    "SELECT * INTO shop.backup FROM shop.orders",
    "SELECT * FROM shop.orders FOR UPDATE",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT lo_create(0)",
    "SELECT pg_sleep(30)",
    "COPY shop.orders TO STDOUT",
    "GRANT ALL ON shop.orders TO PUBLIC",
    "DO $$ BEGIN DELETE FROM shop.orders; END $$",
]

FINGERPRINT = """
    SELECT (SELECT md5(string_agg(c::text, ',' ORDER BY id)) FROM shop.customers c) AS customers,
           (SELECT md5(string_agg(p::text, ',' ORDER BY id)) FROM shop.products p) AS products,
           (SELECT md5(string_agg(o::text, ',' ORDER BY id)) FROM shop.orders o) AS orders,
           (SELECT md5(string_agg(i::text, ',' ORDER BY id)) FROM shop.order_items i) AS order_items
"""


@pytest.mark.anyio
@pytest.mark.parametrize("sql", ATTACKS)
async def test_run_select_query_rejects_writes(client, sql):
    result = await client.call_tool("run_select_query", {"sql": sql})
    assert result.is_error is True
    assert result.structured_content is None


@pytest.mark.anyio
async def test_data_is_unchanged_after_every_attack(client, db):
    before = db.fetch(FINGERPRINT).rows
    for sql in ATTACKS:
        await client.call_tool("run_select_query", {"sql": sql})
    assert db.fetch(FINGERPRINT).rows == before
