"""Validation for free-form SQL coming from an agent.

This is a usability layer, not the security boundary. Its job is to turn an
obviously wrong request (an UPDATE, two statements, pg_sleep) into a clear,
early error the model can act on. The guarantees come from the database:
the SELECT-only role, the READ ONLY transaction and the statement timeout
(see db.py and db/init/03_readonly_role.sh). A query that slips past this
parser still cannot write anything.

The SQL that runs is the agent's original text, not sqlglot's re-generated
version: re-generating could subtly change the meaning of a valid query.
"""

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# sqlglot logs a warning when it falls back to an opaque Command node. We
# reject those anyway, so the warning is noise on the server's stderr.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_QUERY_LENGTH = 10_000

# Nodes that must not appear anywhere in the tree, including inside CTEs and
# subqueries. Command is what sqlglot produces for syntax it does not model
# (EXPLAIN ANALYZE, DO blocks, ...), so unknown means rejected.
_FORBIDDEN_NODES: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
    exp.Create: "CREATE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.Copy: "COPY",
    exp.Into: "SELECT ... INTO",
    exp.Lock: "row locking (FOR UPDATE / FOR SHARE)",
    exp.Command: "this kind of statement",
}

# Functions with side effects or access beyond the shop tables: server admin
# and file access (pg_*), large objects (lo_*), remote connections (dblink),
# changing settings (set_config), sequences, and *_to_xml helpers that run a
# SQL string passed as an argument.
_FORBIDDEN_FUNCTION = re.compile(
    r"^(pg_|lo_|dblink|set_config$|nextval$|setval$|(query|cursor|table|schema|database)_to_xml)",
    re.IGNORECASE,
)


class UnsafeQueryError(ValueError):
    """The SQL was rejected. The message explains why, for the agent."""


def validate_select(query: str) -> str:
    """Check that ``query`` is a single read-only SELECT.

    Returns the query with surrounding whitespace and trailing semicolons
    removed, ready to run. Raises UnsafeQueryError otherwise.
    """
    cleaned = query.strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQueryError("The query is empty.")
    if len(cleaned) > MAX_QUERY_LENGTH:
        raise UnsafeQueryError(f"The query is longer than {MAX_QUERY_LENGTH} characters.")

    try:
        statements = [s for s in sqlglot.parse(cleaned, read="postgres") if s is not None]
    except SqlglotError as exc:
        raise UnsafeQueryError(f"Could not parse the query as PostgreSQL: {_first_line(exc)}") from exc

    if len(statements) != 1:
        raise UnsafeQueryError("Send exactly one statement; multiple statements are not allowed.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeQueryError("Only SELECT queries are allowed (WITH ... SELECT and UNION are fine).")

    for node in tree.walk():
        for node_type, label in _FORBIDDEN_NODES.items():
            if isinstance(node, node_type):
                raise UnsafeQueryError(f"{label} is not allowed; this server is read-only.")
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if _FORBIDDEN_FUNCTION.match(name):
                raise UnsafeQueryError(f"The function {name}() is not allowed.")

    return cleaned


def _function_name(node: exp.Func) -> str:
    # Functions sqlglot does not know become Anonymous and keep their name.
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    return node.sql_name().lower()


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0]
