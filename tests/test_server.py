"""The server as an agent sees it, through a real MCP client session."""

import sys
from datetime import date
from decimal import Decimal

import pytest
from mcp import Client, StdioServerParameters

from .conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "list_tables",
    "describe_table",
    "find_customers",
    "get_customer_orders",
    "top_products",
    "revenue_summary",
    "run_select_query",
}


async def call(client, name, args=None):
    """Call a tool and return its structured result, failing on tool errors."""
    result = await client.call_tool(name, args or {})
    assert result.is_error is False, result.content[0].text
    return result.structured_content


async def call_error(client, name, args) -> str:
    """Call a tool that should fail and return the message the model sees."""
    result = await client.call_tool(name, args)
    assert result.is_error is True
    return result.content[0].text


# --- Discovery ----------------------------------------------------------------


async def test_lists_all_tools_as_read_only(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, tool.name
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.output_schema is not None


async def test_injected_context_is_not_part_of_the_input_schema(client):
    for tool in (await client.list_tools()).tools:
        assert "ctx" not in tool.input_schema.get("properties", {}), tool.name


async def test_input_schema_carries_constraints(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    props = tools["top_products"].input_schema["properties"]
    assert props["period"]["enum"] == ["last_7_days", "last_30_days", "last_90_days", "last_365_days", "all_time"]
    assert props["limit"]["maximum"] == 50


async def test_server_sends_usage_instructions(client):
    assert "Revenue only" in client.instructions
    assert client.server_info.name == "shop-mcp"


# --- Schema tools -------------------------------------------------------------


async def test_list_tables(client):
    result = await call(client, "list_tables")
    assert [t["name"] for t in result["tables"]] == ["customers", "order_items", "orders", "products"]


async def test_describe_table_includes_columns_and_relations(client):
    result = await call(client, "describe_table", {"table_name": "order_items"})
    columns = {c["name"]: c for c in result["columns"]}
    assert columns["id"]["primary_key"] is True
    assert columns["unit_price"]["type"] == "numeric(10,2)"
    assert "time of purchase" in columns["unit_price"]["description"]
    assert {(fk["column"], fk["references_table"]) for fk in result["foreign_keys"]} == {
        ("order_id", "orders"),
        ("product_id", "products"),
    }


async def test_describe_table_unknown_table_lists_alternatives(client):
    message = await call_error(client, "describe_table", {"table_name": "invoices"})
    assert "Available tables: customers, order_items, orders, products" in message


async def test_describe_table_hides_tables_outside_the_shop_schema(client):
    message = await call_error(client, "describe_table", {"table_name": "pg_authid"})
    assert "Unknown table" in message


async def test_describe_table_rejects_non_identifier_input(client):
    await call_error(client, "describe_table", {"table_name": "orders; DROP TABLE orders"})


# --- Customers --------------------------------------------------------------


async def test_find_then_get_customer_orders(client):
    found = await call(client, "find_customers", {"search": "maya.schmidt"})
    customer_id = found["customers"][0]["id"]

    result = await call(client, "get_customer_orders", {"customer_id": customer_id, "limit": 50})
    assert result["customer"]["full_name"] == "Maya Schmidt"
    assert result["truncated"] is False
    assert len(result["orders"]) == result["order_count"]

    # lifetime_value applies the revenue rule to the same orders we got back.
    counted = [o for o in result["orders"] if o["status"] in {"paid", "shipped", "delivered"}]
    assert Decimal(result["lifetime_value"]) == sum(Decimal(o["total"]) for o in counted)

    # Each order total is the sum of its lines.
    for order in result["orders"]:
        lines = sum(Decimal(i["unit_price"]) * i["quantity"] for i in order["items"])
        assert Decimal(order["total"]) == lines


async def test_get_customer_orders_filters_by_status(client):
    result = await call(client, "get_customer_orders", {"customer_id": 2, "status": "cancelled"})
    assert result["orders"]
    assert {o["status"] for o in result["orders"]} == {"cancelled"}


async def test_get_customer_orders_unknown_customer_points_to_find_customers(client):
    message = await call_error(client, "get_customer_orders", {"customer_id": 99_999})
    assert "find_customers" in message


async def test_find_customers_treats_wildcards_literally(client):
    result = await call(client, "find_customers", {"search": "%_"})
    assert result["customers"] == []


# --- Sales --------------------------------------------------------------------


async def test_top_products_ranked_by_revenue(client):
    result = await call(client, "top_products", {"period": "all_time", "limit": 5})
    products = result["products"]
    assert [p["rank"] for p in products] == [1, 2, 3, 4, 5]
    revenues = [Decimal(p["revenue"]) for p in products]
    assert revenues == sorted(revenues, reverse=True)


async def test_top_products_by_units_within_a_category(client):
    result = await call(client, "top_products", {"period": "all_time", "ranked_by": "units", "category": "books"})
    assert {p["category"] for p in result["products"]} == {"Books"}
    units = [p["units_sold"] for p in result["products"]]
    assert units == sorted(units, reverse=True)


async def test_top_products_unknown_category_lists_known_ones(client):
    message = await call_error(client, "top_products", {"category": "Garden"})
    assert "Known categories:" in message and "Electronics" in message


async def test_top_products_rejects_unknown_period(client):
    await call_error(client, "top_products", {"period": "yesterday"})


async def test_revenue_summary_matches_independent_sql(client, db):
    args = {"start_date": "2026-01-01", "end_date": "2026-03-31", "group_by": "month"}
    result = await call(client, "revenue_summary", args)

    expected = db.fetch(
        """
        SELECT sum(oi.quantity * oi.unit_price) AS revenue, count(DISTINCT o.id) AS orders
        FROM shop.orders o JOIN shop.order_items oi ON oi.order_id = o.id
        WHERE o.status IN ('paid', 'shipped', 'delivered')
          AND o.ordered_at >= '2026-01-01' AND o.ordered_at < '2026-04-01'
        """
    ).rows[0]
    assert Decimal(result["revenue"]) == expected["revenue"]
    assert result["orders"] == expected["orders"]

    assert [p["period_start"] for p in result["breakdown"]] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert sum(Decimal(p["revenue"]) for p in result["breakdown"]) == Decimal(result["revenue"])


async def test_revenue_summary_end_date_is_inclusive(client):
    day = date(2026, 3, 15).isoformat()
    result = await call(client, "revenue_summary", {"start_date": day, "end_date": day, "group_by": "day"})
    assert all(p["period_start"] == day for p in result["breakdown"])


async def test_revenue_summary_rejects_reversed_range(client):
    message = await call_error(client, "revenue_summary", {"start_date": "2026-02-01", "end_date": "2026-01-01"})
    assert "before start_date" in message


# --- Free-form SQL --------------------------------------------------------------


async def test_run_select_query(client):
    sql = "SELECT category, count(*) AS products FROM products GROUP BY 1 ORDER BY 1"
    result = await call(client, "run_select_query", {"sql": sql})
    assert result["columns"] == ["category", "products"]
    assert result["row_count"] == 5
    assert result["truncated"] is False


async def test_run_select_query_reports_truncation(client):
    result = await call(client, "run_select_query", {"sql": "SELECT id FROM orders", "max_rows": 10})
    assert result["row_count"] == 10
    assert result["truncated"] is True


async def test_run_select_query_cannot_raise_max_rows_above_the_server_limit(client):
    # The server limit (200 by default) is part of the input schema.
    await call_error(client, "run_select_query", {"sql": "SELECT 1", "max_rows": 10_000})


async def test_run_select_query_passes_postgres_errors_to_the_model(client):
    message = await call_error(client, "run_select_query", {"sql": "SELECT nope FROM orders"})
    assert 'column "nope" does not exist' in message


async def test_run_select_query_timeout_is_reported(client):
    # generate_series is allowed; a cross join of it is just slow.
    sql = "SELECT count(*) FROM generate_series(1, 100000000) a"
    message = await call_error(client, "run_select_query", {"sql": sql})
    assert "statement timeout" in message


# --- Resource -----------------------------------------------------------------


async def test_schema_resource(client):
    resources = (await client.list_resources()).resources
    assert [(r.uri, r.mime_type) for r in resources] == [("schema://shop", "text/markdown")]

    contents = (await client.read_resource("schema://shop")).contents
    assert "## orders" in contents[0].text
    assert "`customer_id` integer (-> customers.id)" in contents[0].text


# --- Real transport -----------------------------------------------------------


async def test_stdio_transport_end_to_end(database_available):
    """Launch the server as a subprocess, the way Claude Desktop / Code does."""
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "shop_mcp"],
        # The child only inherits a minimal environment, so pass config explicitly.
        env={"SHOP_MCP_DATABASE_URL": TEST_DATABASE_URL, "SHOP_MCP_LOG_LEVEL": "WARNING"},
    )
    async with Client(server) as client:
        assert {t.name for t in (await client.list_tools()).tools} == EXPECTED_TOOLS
        result = await call(client, "list_tables")
        assert len(result["tables"]) == 4
