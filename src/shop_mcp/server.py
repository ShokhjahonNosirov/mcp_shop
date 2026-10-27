"""MCP wiring: the tools and resource an agent sees, and how the server runs.

The docstring of each tool is the description the model reads when deciding
whether to call it, and each parameter's type and Field become the JSON
input schema. The SDK validates arguments against that schema before the
function runs, so the code below only receives well-formed input.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from shop_mcp import models, queries
from shop_mcp.config import Settings
from shop_mcp.db import Database, QueryError
from shop_mcp.sql_guard import MAX_QUERY_LENGTH, UnsafeQueryError

INSTRUCTIONS = """\
Read-only access to the database of a small online shop: customers, products,
orders and order_items. Nothing can be changed through this server.

Prefer the domain tools (find_customers, get_customer_orders, top_products,
revenue_summary): they apply the shop's business rules for you. Revenue only
counts orders with status paid, shipped or delivered. Amounts are in EUR and
timestamps are UTC.

Use list_tables and describe_table to learn the schema, and run_select_query
only for questions the domain tools cannot answer. Results are capped; when a
result says truncated=true, narrow the question instead of assuming you saw
everything.
"""

# Every tool here only reads, gives the same answer for the same input, and
# touches nothing outside this database. Clients may use these hints, for
# example to skip confirmation prompts, but they are hints, not security.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


@dataclass
class AppContext:
    db: Database


def create_server(settings: Settings) -> MCPServer[AppContext]:
    # Created here, opened by the lifespan: the pool exists only while the
    # server runs, but the object is also reachable from the resource below.
    db = Database(settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        # Runs once at startup and shutdown, not per request: one shared
        # connection pool for the life of the server.
        await anyio.to_thread.run_sync(db.open)
        try:
            yield AppContext(db=db)
        finally:
            await anyio.to_thread.run_sync(db.close)

    mcp = MCPServer(
        "shop-mcp",
        title="Shop database (read-only)",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        lifespan=lifespan,
        log_level=settings.log_level,
    )

    # Tools are plain `def`: the SDK runs them in a worker thread, which suits
    # the blocking psycopg driver. ctx is injected by the SDK and is not part
    # of the input schema the model sees.

    @mcp.tool(title="List tables", annotations=READ_ONLY)
    def list_tables(ctx: Context[AppContext]) -> models.TableList:
        """List the tables you can query, with a short description and approximate row count.

        Start here when you need to know what data exists.
        """
        with _tool_errors():
            return queries.list_tables(_db(ctx))

    @mcp.tool(title="Describe table", annotations=READ_ONLY)
    def describe_table(
        table_name: Annotated[
            str,
            Field(description="Table name from list_tables, e.g. 'orders'.", pattern=r"^[a-z_][a-z0-9_]*$", max_length=63),
        ],
        ctx: Context[AppContext],
    ) -> models.TableDescription:
        """Show a table's columns (type, nullability, meaning) and how it joins to other tables.

        Use this before writing SQL for run_select_query.
        """
        with _tool_errors():
            return queries.describe_table(_db(ctx), table_name)

    @mcp.tool(title="Find customers", annotations=READ_ONLY)
    def find_customers(
        search: Annotated[
            str,
            Field(description="Part of the customer's name or email, case-insensitive.", min_length=2, max_length=100),
        ],
        ctx: Context[AppContext],
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum number of customers to return.")] = 10,
    ) -> models.CustomerSearch:
        """Look up customers by name or email to get their customer id.

        Use this when the user names a customer; other tools need the id.
        """
        with _tool_errors():
            return queries.find_customers(_db(ctx), search, limit)

    @mcp.tool(title="Get customer orders", annotations=READ_ONLY)
    def get_customer_orders(
        customer_id: Annotated[int, Field(ge=1, description="Customer id, from find_customers.")],
        ctx: Context[AppContext],
        status: Annotated[
            models.OrderStatus | None,
            Field(description="Only return orders with this status. Omit for all statuses."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum number of orders to return.")] = 20,
    ) -> models.CustomerOrders:
        """Get a customer's profile, lifetime value and most recent orders with their line items.

        Use this for questions about one customer: what they bought, when,
        how much they spent, or the state of their orders.
        """
        with _tool_errors():
            return queries.get_customer_orders(_db(ctx), customer_id, status, limit)

    @mcp.tool(title="Top products", annotations=READ_ONLY)
    def top_products(
        ctx: Context[AppContext],
        period: Annotated[models.Period, Field(description="Time window, counted back from now.")] = "last_30_days",
        ranked_by: Annotated[
            Literal["revenue", "units"],
            Field(description="Rank by revenue (EUR) or by number of units sold."),
        ] = "revenue",
        category: Annotated[
            str | None,
            Field(description="Only rank products in this category, e.g. 'Books'. Omit for all.", max_length=100),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many products to return.")] = 10,
    ) -> models.TopProducts:
        """Rank the best-selling products over a recent period.

        Only paid, shipped and delivered orders count.
        """
        with _tool_errors():
            return queries.top_products(_db(ctx), period, ranked_by, category, limit)

    @mcp.tool(title="Revenue summary", annotations=READ_ONLY)
    def revenue_summary(
        start_date: Annotated[date, Field(description="First day to include (YYYY-MM-DD, UTC).")],
        end_date: Annotated[date, Field(description="Last day to include (YYYY-MM-DD, UTC), inclusive.")],
        ctx: Context[AppContext],
        group_by: Annotated[
            Literal["none", "day", "week", "month", "quarter"],
            Field(description="Also break revenue down by this period. 'none' returns totals only."),
        ] = "month",
    ) -> models.RevenueSummary:
        """Summarise revenue between two dates: total, order count, average order value,
        distinct customers, cancellations and refunds, optionally broken down over time.

        Only paid, shipped and delivered orders count as revenue.
        """
        with _tool_errors():
            return queries.revenue_summary(_db(ctx), start_date, end_date, group_by)

    @mcp.tool(title="Run SELECT query", annotations=READ_ONLY)
    def run_select_query(
        sql: Annotated[
            str,
            Field(
                description="A single PostgreSQL SELECT statement (WITH and UNION are allowed). "
                "Tables are in the shop schema, which is on the search path.",
                min_length=1,
                max_length=MAX_QUERY_LENGTH,
            ),
        ],
        ctx: Context[AppContext],
        max_rows: Annotated[
            int,
            Field(ge=1, le=settings.max_rows, description="Maximum rows to return."),
        ] = min(100, settings.max_rows),
    ) -> models.QueryRows:
        """Run one read-only SQL SELECT and return the rows.

        Use this only when no other tool answers the question. Check the
        schema with describe_table first. Writes, multiple statements and
        server functions are rejected, and slow queries are cancelled.
        Prefer aggregates (count, sum, GROUP BY) over fetching raw rows.
        """
        with _tool_errors():
            return queries.run_select_query(_db(ctx), sql, max_rows)

    # A resource, unlike a tool, is chosen by the user or the client app, not
    # by the model: for example attached as context with an @-mention.
    # The SDK does not inject Context into static (template-free) resources,
    # so this one uses the db from the enclosing scope.
    @mcp.resource(
        "schema://shop",
        name="shop_schema",
        title="Shop database schema",
        description="Every table and column the server exposes, as Markdown.",
        mime_type="text/markdown",
    )
    def shop_schema() -> str:
        return queries.schema_overview(db)

    return mcp


def _db(ctx: Context[AppContext]) -> Database:
    return ctx.request_context.lifespan_context.db


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Turn expected failures into ToolError, which the model reads and can act on.

    Anything else is a bug: the SDK logs it and the client only gets a
    generic error, so internals never leak to the agent.
    """
    try:
        yield
    except (QueryError, UnsafeQueryError, queries.RequestError) as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    settings = Settings()
    mcp = create_server(settings)
    if settings.transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="streamable-http",
            host=settings.http_host,
            port=settings.http_port,
            # No server-to-client streaming or session state is needed for
            # read-only lookups, so every request can be handled independently.
            stateless_http=True,
            json_response=True,
        )
