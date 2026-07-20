"""Run SQL against a database URL in a single transaction.

Pure logic: execute, commit/rollback, and return a description of what
happened. Formatting the result for the terminal is the command layer's job.
"""

from dataclasses import dataclass

import psycopg2

from .. import app
from .split import split_statements


@dataclass
class QueryResult:
    # kind is one of:
    #   "rows"  -> a result set (SELECT / ... RETURNING); columns + rows set
    #   "count" -> a write with no result set (INSERT/UPDATE/DELETE); rowcount set
    #   "ok"    -> a statement with no rowcount (DDL such as CREATE/ALTER)
    kind: str
    columns: list = None
    rows: list = None
    rowcount: int = None


@dataclass
class StatementResult:
    """One statement's outcome under --multi.

    `preview` is the statement's own first line (truncated) — the caller wrote
    it, so echoing it back discloses nothing.
    """
    index: int          # 1-based position among the executed statements
    preview: str
    result: QueryResult


class StatementError(Exception):
    """Statement `index` of `total` failed; the driver's error is __cause__.

    index/total derive only from the caller's own input, so str(self) is safe
    to disclose even in system mode. The cause is NOT baked into the message:
    the command layer routes it through `server_error`, same as ever.
    """

    def __init__(self, index: int, total: int):
        super().__init__(f"statement {index} of {total} failed")
        self.index = index
        self.total = total


def _connect(database_url: str):
    # In read-only apps (explore-db) the server itself rejects any write: a
    # read-only transaction fails on INSERT/UPDATE/DELETE/DDL, so the guarantee
    # does not depend on parsing the SQL. Committing a read-only transaction is
    # harmless, so the surrounding flow stays identical for both apps.
    connect_kwargs = {"sslmode": "require"}
    if app.current().read_only:
        connect_kwargs["options"] = "-c default_transaction_read_only=on"
    return psycopg2.connect(database_url, **connect_kwargs)


def _classify(cur) -> QueryResult:
    """What one just-executed statement produced, read off the cursor."""
    if cur.description is not None:
        columns = [desc[0] for desc in cur.description]
        return QueryResult("rows", columns=columns, rows=cur.fetchall())
    if cur.rowcount >= 0:
        return QueryResult("count", rowcount=cur.rowcount)
    # rowcount is -1 when undefined (e.g. DDL such as CREATE/ALTER).
    return QueryResult("ok")


def run_query(database_url: str, sql: str) -> QueryResult:
    conn = _connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            result = _classify(cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _preview(stmt: str, limit: int = 80) -> str:
    line = stmt.splitlines()[0] if stmt else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


def run_multi(database_url: str, sql: str) -> "list[StatementResult]":
    """Split `sql` and run each statement on one cursor in one transaction.

    Same atomicity as run_query — commit once at the end, roll everything back
    on any failure. Embedded BEGIN/COMMIT are executed as-is: the server
    enforces their semantics, exactly as it does under single-execute.
    """
    statements = split_statements(sql)
    conn = _connect(database_url)
    try:
        results = []
        with conn.cursor() as cur:
            for index, stmt in enumerate(statements, 1):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    raise StatementError(index, len(statements)) from e
                results.append(
                    StatementResult(index, _preview(stmt), _classify(cur)))
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _server_message(exc: Exception) -> "str | None":
    """One exception's own server complaint, or None if it is not one.

    The rule, which `server_error` below applies to a whole chain: a SQLSTATE
    (`pgcode`) means the server answered, and only what the server said may be
    disclosed. Without one, disclose nothing.

    Both conditions are load-bearing even though measured psycopg2 errors happen
    to satisfy them together: `pgcode` is the documented "the server answered"
    signal, and an empty `diag` must never render as "None".

    Built from `diag`, never `str(exc)`: str() of a server error also carries
    LINE/caret context, and restricting the result to the server's own primary
    message (plus its hint, which is guidance, not data) means nothing except
    the server's words can ever escape.
    """
    if getattr(exc, "pgcode", None) is None:
        return None
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if not primary:
        return None
    hint = getattr(diag, "message_hint", None)
    return f"{primary} ({hint})" if hint else primary


def server_error(exc: Exception) -> "str | None":
    """The server's own complaint about a statement, or None if it wasn't one.

    Splits psycopg2 failures into the only two kinds that matter for disclosure:

    - A **server-side** error carries a SQLSTATE (`pgcode`) and a
      `diag.message_primary` describing the statement — 'syntax error at or near
      "SELEKT"', 'relation "users" does not exist'. It names nothing but the
      caller's own SQL, so it is safe to hand back even over sudo.
    - A **connection-level** failure has no SQLSTATE, and its text can echo the
      connection string: 'could not translate host name "db-internal" to
      address'. That is the leak the hardened path exists to prevent, so the
      caller keeps withholding it.

    The whole __context__ chain is searched, not just the top. Every caller ends
    its transaction in an except/finally, so a backend the server terminated
    raises TWICE: the real OperationalError from execute(), then an
    InterfaceError from the rollback that tried to tidy up after it — and the
    second, which has no SQLSTATE, is what propagates. The server's own words
    survive only as `__context__`, and reporting a bare "failed" while they sit
    there is precisely what this split exists NOT to do.

    Searching deeper does not disclose more: every link faces the same
    `_server_message` test, so a connection error stays opaque wherever in the
    chain it sits. `__context__` (implicit chaining) is what a raising
    except/finally produces; nothing here raises `from`, so `__cause__` would
    find nothing `__context__` does not.
    """
    # CPython breaks context cycles when it chains implicitly, and psycopg2
    # never assigns __context__ by hand, so `seen` guards a shape production
    # cannot currently produce. It stays anyway: this is the disclosure gate,
    # and two lines to ensure it always terminates beats depending on CPython's
    # chaining rules holding for every exception that ever reaches it.
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        message = _server_message(exc)
        if message:
            return message
        # Only when the top has nothing to say: the top IS the error being
        # reported, so an older one underneath must never replace it.
        exc = exc.__context__
    return None
