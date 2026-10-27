"""The only way the server talks to PostgreSQL.

Every query goes through ``Database.fetch``, which applies the same rules no
matter which tool called it:

* a READ ONLY transaction, always rolled back at the end;
* ``statement_timeout`` set for that transaction only;
* a server-side cursor, so at most ``max_rows + 1`` rows ever leave the
  database (the extra row tells us the result was truncated).

Sync psycopg is used on purpose. The MCP SDK runs plain ``def`` tools in
worker threads, and psycopg's async mode does not work with the default
event loop on Windows, where Claude Desktop may launch this server.
"""

import logging
from dataclasses import dataclass
from typing import Any, LiteralString

import psycopg
from psycopg import errors, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from shop_mcp.config import Settings

log = logging.getLogger(__name__)

Query = LiteralString | sql.Composable


class QueryError(Exception):
    """A query failed. The message is safe to show to the agent."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool


class Database:
    def __init__(self, settings: Settings) -> None:
        self.max_rows = settings.max_rows
        self.statement_timeout_ms = settings.statement_timeout_ms
        self._pool = ConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            configure=_configure_connection,
            check=ConnectionPool.check_connection,
            open=False,
        )

    def open(self, timeout: float = 10.0) -> None:
        """Open the pool and wait until it has a working connection.

        Failing here, at startup, gives a clear error instead of every tool
        call failing later with a confusing one.
        """
        self._pool.open(wait=True, timeout=timeout)

    def close(self) -> None:
        self._pool.close()

    def fetch(
        self,
        query: Query,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int | None = None,
    ) -> QueryResult:
        """Run one read-only query and return at most ``max_rows`` rows."""
        limit = min(max_rows or self.max_rows, self.max_rows)
        try:
            with self._pool.connection(timeout=5.0) as conn:
                # force_rollback: nothing a query does can outlive it. That
                # includes session settings changed with set_config(), which
                # PostgreSQL reverts when the transaction rolls back, so a
                # pooled connection is always clean for the next caller.
                with conn.transaction(force_rollback=True):
                    conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(self.statement_timeout_ms),),
                    )
                    # A named cursor is a server-side DECLARE ... CURSOR.
                    # PostgreSQL only accepts a SELECT or VALUES there, and
                    # rows are streamed on demand instead of all at once.
                    with conn.cursor(name="mcp_query", row_factory=dict_row) as cur:
                        cur.execute(query, params)
                        rows = cur.fetchmany(limit + 1)
                        columns = [col.name for col in cur.description or []]
        except PoolTimeout as exc:
            raise QueryError("The database is busy or unavailable. Try again shortly.") from exc
        except errors.QueryCanceled as exc:
            raise QueryError(
                f"Query cancelled: it exceeded the {self.statement_timeout_ms} ms statement timeout. "
                "Narrow it down with filters or aggregates."
            ) from exc
        except (errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege) as exc:
            raise QueryError(f"Rejected: this server is read-only ({_message(exc)}).") from exc
        except errors.OperationalError as exc:
            # Connection problems: log the details, do not show host names
            # or credentials to the agent.
            log.error("Database connection error: %s", exc)
            raise QueryError("The database is unavailable.") from exc
        except psycopg.Error as exc:
            # Syntax errors, unknown columns, etc. The agent can fix these,
            # so pass PostgreSQL's own message through.
            raise QueryError(_message(exc)) from exc

        return QueryResult(columns=columns, rows=rows[:limit], truncated=len(rows) > limit)


def _configure_connection(conn: psycopg.Connection) -> None:
    # Every transaction psycopg starts on this connection is BEGIN READ ONLY.
    # The role's default_transaction_read_only does the same server-side;
    # setting it here too means we do not depend on how the role was set up.
    conn.read_only = True


def _message(exc: psycopg.Error) -> str:
    return exc.diag.message_primary or str(exc).strip()
