# shop-mcp

A **read-only [MCP](https://modelcontextprotocol.io) server** that gives AI agents such as Claude safe, structured access to a PostgreSQL database.

The database is a small fictional online shop with customers, products, orders and order items. The server exposes it through a few **domain-specific tools**, such as revenue summaries, best sellers and a customer's order history, plus schema discovery and a guarded `SELECT` tool for anything else.

Read-only is enforced by the database itself, at several independent layers. It doesn't depend on the agent behaving well.

- Python 3.13, the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) v2 (`MCPServer`)
- PostgreSQL 17, psycopg 3 with a connection pool, sqlglot
- Docker Compose, uv, pytest (125 tests)

---

## Contents

- [What the agent gets](#what-the-agent-gets)
- [Quick start](#quick-start)
- [Connecting Claude](#connecting-claude)
- [Architecture](#architecture)
- [Security model](#security-model)
- [Configuration](#configuration)
- [Example conversation](#example-conversation)
- [Development](#development)
- [Design notes](#design-notes)

---

## What the agent gets

### Tools

Tools are functions that the model decides to call.

| Tool | Purpose | Key inputs |
|---|---|---|
| `list_tables` | What data exists: tables, descriptions, approximate row counts | – |
| `describe_table` | Columns, types, meaning, primary key, and how the table joins to others | `table_name` |
| `find_customers` | Look up a customer id by name or email | `search`, `limit` |
| `get_customer_orders` | A customer's profile, lifetime value and recent orders with line items | `customer_id`, `status?`, `limit` |
| `top_products` | Best sellers over a period, by revenue or by units | `period` (enum), `ranked_by`, `category?`, `limit` |
| `revenue_summary` | Revenue, orders, average order value, cancellations and refunds between two dates, optionally per day/week/month/quarter | `start_date`, `end_date`, `group_by` |
| `run_select_query` | One validated, row-limited `SELECT`, for questions the other tools don't cover | `sql`, `max_rows` |

Each tool has:

- **A description written for the model**, saying when to use the tool and when not to. Example: *"Use this only when no other tool answers the question."*
- **A typed input schema** with constraints: enums for periods and statuses, numeric bounds, string patterns. The SDK validates arguments against the schema **before** any code runs, so the agent gets a precise error such as `Input should be less than or equal to 50` and can retry.
- **A typed output schema** built from Pydantic models. Money is `Decimal`, serialised as `"79.90"`, never a float.
- **`readOnlyHint` / `idempotentHint` annotations**, so clients can tell the tools are safe to call. They are hints only; they aren't what makes the server read-only.

The domain tools contain the **business rules**, so the agent doesn't have to guess them. For example, revenue counts only `paid`, `shipped` and `delivered` orders, not pending, cancelled or refunded ones.

### Resource

`schema://shop` is the whole schema as Markdown. The difference from a tool is who decides to use it: tools are **model-controlled**, while resources are **application-controlled**. A user or a client app attaches a resource as context, for example with an `@`-mention in Claude Code.

### Server instructions

When it connects, the client receives short usage instructions: prefer the domain tools, revenue rules, EUR, UTC, and what `truncated: true` means.

---

## Quick start

Requirements: Docker with Compose. For local development and tests, also [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd shop-mcp
docker compose up -d --build --wait
```

That starts two services:

| Service | What | Where |
|---|---|---|
| `db` | PostgreSQL 17, seeded on first start | `127.0.0.1:55432` |
| `mcp` | the MCP server, Streamable HTTP transport | `http://127.0.0.1:8000/mcp` |

The seed creates 80 customers, 30 products, 1,500 orders and about 3,500 order items over the last 18 months. Everything is invented: emails use the reserved `example.com` domain.

To reset the data, run `docker compose down -v && docker compose up -d --wait`.

---

## Connecting Claude

MCP has two standard **transports**. Both carry the same JSON-RPC messages:

- **stdio:** the host (Claude Desktop or Claude Code) launches the server as a subprocess and talks to it over stdin and stdout. Simple and local, with no network port.
- **Streamable HTTP:** the server is a long-running HTTP service, and the host sends requests to `/mcp`. Suits Docker and shared or remote deployments.

On connection, the client and server run an `initialize` handshake to exchange protocol version, capabilities and instructions. The client then calls `tools/list` and shows the tools to the model. When the model decides to use a tool, the client sends `tools/call`.

### Claude Code: HTTP (Docker)

This repo includes a project-scoped [`.mcp.json`](.mcp.json):

```json
{ "mcpServers": { "shop": { "type": "http", "url": "http://127.0.0.1:${SHOP_MCP_HOST_PORT:-8000}/mcp" } } }
```

Start the stack, run `claude` inside this directory, and approve the `shop` server when asked. Check it with `/mcp` or `claude mcp list`.

To add it outside this directory:

```bash
claude mcp add --transport http shop http://127.0.0.1:8000/mcp
```

### Claude Code: stdio (no server container)

Only the database needs to run (`docker compose up -d db`):

```bash
claude mcp add --env SHOP_MCP_DATABASE_URL=postgresql://mcp_readonly:mcp_readonly_pw@127.0.0.1:55432/shop \
  --transport stdio shop -- uv --directory /absolute/path/to/shop-mcp run shop-mcp
```

### Claude Desktop: stdio

Claude Desktop's config file starts **local stdio** servers. Remote servers are added as Connectors, which need a publicly reachable URL, so `localhost` won't work there.

Edit `claude_desktop_config.json`. On macOS it's in `~/Library/Application Support/Claude/`, on Windows in `%APPDATA%\Claude\`.

```json
{
  "mcpServers": {
    "shop": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/shop-mcp", "run", "shop-mcp"],
      "env": {
        "SHOP_MCP_DATABASE_URL": "postgresql://mcp_readonly:mcp_readonly_pw@127.0.0.1:55432/shop"
      }
    }
  }
}
```

On Windows, use a path like `"E:\\path\\to\\shop-mcp"`. If Desktop can't find `uv`, set `"command"` to its absolute path; `where uv` or `which uv` prints it. Fully quit and reopen Claude Desktop afterwards. Logs are in `mcp-server-shop.log`, under `~/Library/Logs/Claude` or `%APPDATA%\Claude\logs`.

> **Why `127.0.0.1` and not `localhost`?** On Windows, `localhost` resolves to IPv6 `::1` first. Docker publishes the port on IPv4 only, so the connection hangs until it times out.

---

## Architecture

```mermaid
flowchart LR
    host["Claude Code / Claude Desktop<br/>(MCP host + client)"]
    subgraph server["shop-mcp (MCPServer)"]
        tools["server.py<br/>tools, resource, instructions<br/>input/output schemas"]
        domain["queries.py<br/>domain SQL + business rules"]
        guard["sql_guard.py<br/>SELECT-only validator"]
        runner["db.py<br/>pool, READ ONLY txn,<br/>timeout, row cap"]
    end
    pg[("PostgreSQL<br/>role: mcp_readonly<br/>SELECT on schema shop")]

    host -- "stdio or Streamable HTTP<br/>JSON-RPC" --> tools
    tools --> domain
    domain -- run_select_query only --> guard
    guard --> runner
    domain --> runner
    runner --> pg
```

| Module | Responsibility |
|---|---|
| [`server.py`](src/shop_mcp/server.py) | MCP wiring: tool definitions, descriptions, annotations, the `schema://shop` resource, lifespan (opens and closes the pool), error mapping, transport selection |
| [`queries.py`](src/shop_mcp/queries.py) | Domain logic. Plain functions `(db, args) -> model`, with no MCP dependency, so they can be tested directly |
| [`models.py`](src/shop_mcp/models.py) | Pydantic result models; these become the tools' output schemas |
| [`sql_guard.py`](src/shop_mcp/sql_guard.py) | Parses agent SQL with sqlglot and rejects anything other than a single SELECT |
| [`db.py`](src/shop_mcp/db.py) | **The only path to the database.** Every query runs with the same protections |
| [`config.py`](src/shop_mcp/config.py) | Typed settings from `SHOP_MCP_*` environment variables |
| [`db/init/`](db/init/) | Schema with table and column comments, deterministic seed, read-only role |

**Request lifecycle**, e.g. `top_products(period="last_30_days")`:

1. The host sends `tools/call`. The SDK validates the arguments against the input schema.
2. The tool reads the shared `Database` from the lifespan context (`ctx.request_context.lifespan_context`) and calls `queries.top_products`.
3. `db.fetch` borrows a pooled connection. It opens a `BEGIN READ ONLY` transaction, sets `statement_timeout`, runs the query through a server-side cursor, fetches at most `max_rows + 1` rows, then **rolls back**.
4. The result is validated against the `TopProducts` output schema and returned to the client in two forms: JSON text for the model, and `structured_content` for applications.
5. Expected failures become a `ToolError`, an `is_error: true` result whose message the model can read and act on. Examples: unknown customer, bad SQL, timeout. Unexpected exceptions are logged on the server, and the client only gets a generic message.

---

## Security model

> **Assume the agent will eventually send something harmful,** through a prompt injection, a confused model or a malicious user. The question is what the database lets it do then. The answer here is: read the `shop` tables and nothing else.

### Defence in depth

| # | Layer | Where | What it stops | Bypassed in tests by |
|---|---|---|---|---|
| 1 | **SQL guard** | `sql_guard.py` | Non-SELECT statements, multiple statements, `SELECT INTO`, `FOR UPDATE`, data-modifying CTEs, `pg_*`/`lo_*`/`dblink`/`set_config`/`*_to_xml` functions. Gives the agent a clear, early error | – |
| 2 | **Query runner** | `db.py` | Runs everything through `DECLARE CURSOR`, which only accepts a single plain query, over the extended protocol, so stacked statements are impossible | calling `db.fetch` directly |
| 3 | **Read-only transaction** | `db.py` + role default | Postgres rejects writes, `SELECT ... FOR UPDATE` and large-object writes, and a transaction can't be switched to read-write mid-way | sending SQL that passes layers 1–2 |
| 4 | **SELECT-only role** | `03_readonly_role.sh` | No INSERT/UPDATE/DELETE/TRUNCATE/DDL privileges, no temp tables, no `public` schema, no server file access. Even an explicit `BEGIN READ WRITE` can't write | a raw psycopg connection in `READ WRITE` mode |
| 5 | **Resource limits** | `db.py` + role | `statement_timeout` per transaction (default 5 s, role backstop 10 s), `idle_in_transaction_session_timeout`, row cap with a `truncated` flag, pool size, `CONNECTION LIMIT 10` | – |

[`tests/test_readonly_enforcement.py`](tests/test_readonly_enforcement.py) attacks **each layer on its own**, with the layers in front of it bypassed. A regression in any single layer therefore fails a test, even while the other layers still hold. It ends with an end-to-end test: 15 attacks go through the real MCP tool, and the test checks that an md5 fingerprint of every table is unchanged afterwards.

### Why each decision

- **The database is the security boundary, not the parser.** SQL parsing is hard, and a validator that disagrees with Postgres's parser on one edge case would be a hole. The guard is there for *usability*: it gives a fast, readable error the model can learn from. The guarantees come from Postgres privileges and transaction modes, which the application can't accidentally turn off.
- **A dedicated role with only the access it needs.** It has `SELECT` on the `shop` schema and nothing more. It isn't a superuser, can't create objects or temp tables, and `CONNECT` and `TEMP` were revoked from `PUBLIC`. The server **never sees the owner credentials**.
- **No `ALTER DEFAULT PRIVILEGES`.** A table added later, say `payment_tokens`, is *not* exposed to agents until someone grants it deliberately. `list_tables` uses `has_table_privilege`, so discovery shows the real grants.
- **Read-only is set both in the role and on the connection.** The role's `default_transaction_read_only = on` protects any client, and psycopg's `conn.read_only = True` means the application doesn't rely on how the role was configured.
- **Every transaction is rolled back.** Nothing a query does can outlive it. That includes session settings changed via `set_config()`, which Postgres reverts on rollback, so a pooled connection is always clean for the next request. This is tested.
- **Timeouts are applied per transaction with `set_config(..., true)`,** so a value can't leak between requests, and the role-level timeout is a backstop.
- **Row limits use a server-side cursor,** so only `max_rows + 1` rows ever leave Postgres, however large the result. The extra row sets `truncated: true`, and the instructions tell the agent to narrow its question instead of assuming it saw everything. `max_rows` is capped by the server setting, and that cap is part of the input schema.
- **Values are always bound as parameters.** The only dynamic identifier is the sort column in `top_products`, chosen from a fixed enum and composed with `psycopg.sql.Identifier`.
- **Errors are sanitised.** PostgreSQL's own message, e.g. `column "x" does not exist`, is passed through so the agent can fix its SQL. Connection errors are logged, but host names and credentials never reach the agent.
- **Config uses the `SHOP_MCP_` prefix.** Developer machines often have a generic `DATABASE_URL` pointing at an admin role, and the server ignores it, which is tested. The URL is a `SecretStr`, so the password stays out of logs and reprs.
- **Hardened container.** It runs as a non-root user with a read-only filesystem, `cap_drop: ALL` and `no-new-privileges`. Ports are published on `127.0.0.1` only.

### Out of scope for a demo, needed in production

- **Authentication on the HTTP endpoint.** Here it is bound to localhost. The SDK supports OAuth 2.1 resource-server mode (`token_verifier` / `AuthSettings`), which a real deployment should use, for example behind an identity provider.
- **Row- or column-level restrictions** with PostgreSQL row-level security or views, e.g. masking customer emails per agent or tenant.
- **Rate limiting and audit logging** of tool calls.
- **Running against a replica**, so agent queries can't load the primary.

---

## Configuration

All settings are environment variables. A `.env` file in the working directory is also read; see [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `SHOP_MCP_DATABASE_URL` | **required** | libpq URL of the **read-only** role |
| `SHOP_MCP_MAX_ROWS` | `200` | Hard cap on rows per query (1–10,000) |
| `SHOP_MCP_STATEMENT_TIMEOUT_MS` | `5000` | Per-query timeout (100–60,000 ms) |
| `SHOP_MCP_POOL_MIN_SIZE` / `_MAX_SIZE` | `1` / `5` | Connection pool size |
| `SHOP_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `SHOP_MCP_HTTP_HOST` / `_PORT` | `127.0.0.1` / `8000` | Bind address for HTTP (the Docker image uses `0.0.0.0`) |
| `SHOP_MCP_LOG_LEVEL` | `INFO` | Logs always go to stderr; on stdio, stdout *is* the protocol |

Compose also reads `POSTGRES_PASSWORD`, `MCP_READONLY_PASSWORD`, `SHOP_DB_HOST_PORT` (default `55432`) and `SHOP_MCP_HOST_PORT` (default `8000`).

Invalid values, such as `SHOP_MCP_MAX_ROWS=0`, stop the server at startup with a clear error.

---

## Example conversation

The tool calls and their results below are real outputs from this server. Claude's replies are illustrative. The data is dated relative to when the database was seeded, so your numbers will differ.

> **You:** How did revenue in Q3 compare to Q2 this year?
>
> *Claude calls* `revenue_summary(start_date="2026-04-01", end_date="2026-09-30", group_by="quarter")`
> → `revenue: "130210.26"`, `breakdown: [{period_start: "2026-04-01", revenue: "57257.13", orders: 245}, {period_start: "2026-07-01", revenue: "72953.13", orders: 300}]`
>
> **Claude:** Q3 revenue was €72,953 from 300 orders, against €57,257 from 245 orders in Q2. That's **+27% revenue** and +22% orders, so the average order value also rose slightly. Only paid, shipped and delivered orders are counted.

> **You:** What sold best in the last 30 days?
>
> *Claude calls* `top_products(period="last_30_days", limit=3)`
>
> **Claude:**
> 1. Aurora Wireless Earbuds: €4,714 (59 units)
> 2. Adjustable Dumbbells (pair): €3,781 (19 units)
> 3. Brewmaster Pour-Over Kettle: €2,538 (47 units)
>
> The dumbbells are second by revenue on far fewer units, because of their €199 price.

> **You:** What has Maya Schmidt ordered recently?
>
> *Claude calls* `find_customers(search="maya")` and gets two matches: Maya Kowalski (#27) and Maya Schmidt (#2).
> *Then* `get_customer_orders(customer_id=2, limit=2)`
>
> **Claude:** Maya Schmidt (Wrocław, customer since May 2024) has placed 25 orders, with a lifetime value of €5,193.42. Her two most recent orders, both delivered:
> - **13 Oct**, €99.80: Aurora Wireless Earbuds, Volt 20W USB-C Charger
> - **1 Oct**, €538.40: 2× Adjustable Dumbbells, Aurora Wireless Earbuds, Atlas of Small Islands, 2× Night Trains of Europe

> **You:** Which shipping country has the highest average order value?
>
> No domain tool answers this, so *Claude calls* `describe_table("orders")`, then:
> ```sql
> SELECT o.shipping_country, count(DISTINCT o.id) AS orders,
>        round(sum(oi.quantity * oi.unit_price) / count(DISTINCT o.id), 2) AS avg_order_value
> FROM orders o JOIN order_items oi ON oi.order_id = o.id
> WHERE o.status IN ('paid', 'shipped', 'delivered')
> GROUP BY 1 ORDER BY 3 DESC LIMIT 3
> ```
>
> **Claude:** Portugal leads at €269.06 per order (74 orders), followed by Spain at €258.50 and Italy at €249.16.

> **You:** Clean up the database: delete all cancelled orders.
>
> *Claude calls* `run_select_query(sql="DELETE FROM orders WHERE status = 'cancelled'")`
> → `is_error: true`: *"Only SELECT queries are allowed (WITH ... SELECT and UNION are fine)."*
>
> **Claude:** I can't do that. This server is read-only by design, and the database account it uses has no permission to change data. I can list the cancelled orders so someone with write access can review them.

---

## Development

```bash
uv sync                       # create .venv with runtime + dev dependencies
docker compose up -d db       # integration tests need the database
uv run pytest                 # 125 tests, ~15 s
```

| Test file | Covers |
|---|---|
| `test_config.py` | Env parsing, the prefix, secret masking, bounds |
| `test_sql_guard.py` | 47 accept/reject cases for the validator (no DB needed) |
| `test_db.py` | Result shape, parameter binding, truncation, timeouts, error messages |
| `test_readonly_enforcement.py` | Each security layer on its own, plus an end-to-end data fingerprint |
| `test_server.py` | Through a real MCP `Client`: tool listing, schemas, annotations, every tool's behaviour and errors, the resource, and the stdio transport as a subprocess |

Integration tests skip, rather than fail, when the database isn't reachable. Use `SHOP_MCP_TEST_DATABASE_URL` to point them at another database.

To run the server locally without Docker:

```bash
cp .env.example .env
uv run shop-mcp                                    # stdio: waits for a client on stdin
SHOP_MCP_TRANSPORT=streamable-http uv run shop-mcp # HTTP on 127.0.0.1:8000/mcp
```

```
shop-mcp/
├── src/shop_mcp/        config, db, sql_guard, queries, models, server
├── db/init/             01_schema.sql, 02_seed.sql, 03_readonly_role.sh
├── tests/
├── Dockerfile           python:3.13-slim + uv, non-root
├── docker-compose.yml   db + mcp
└── .mcp.json            Claude Code project config
```

---

## Design notes

- **MCP SDK v2.** Uses `MCPServer` (renamed from `FastMCP` in v2), `ToolError` for errors the model can fix, and `Client(server)` for in-memory tests.
- **Sync psycopg, not async.** The SDK runs plain `def` tools in worker threads. psycopg's async mode doesn't support the default event loop on Windows, where Claude Desktop may launch the server. With a pool and short queries, threads cost nothing noticeable.
- **Domain logic is separate from MCP.** `queries.py` has no MCP imports. The same functions could back a REST API or a CLI, and `server.py` stays a thin layer of descriptions and schemas.
- **Stateless HTTP.** Read-only lookups need no server-to-client streaming or session state, so `stateless_http=True, json_response=True`. Any replica can serve any request.
- **Deterministic seed.** Instead of `random()`, the seed hashes a label with `md5`, so every fresh database has the same customers, bestsellers and revenue curve. Dates are relative to seeding time, so "last 30 days" always has data.
- **Schema comments as documentation.** `COMMENT ON` text, such as *"Price in EUR at the time of purchase"*, is returned by `describe_table`, so the agent learns what columns *mean*, not just their types.
