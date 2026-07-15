"""Run SQL against a database URL in a single transaction.

Pure logic: execute, commit/rollback, and return a description of what
happened. Formatting the result for the terminal is the command layer's job.
"""

from dataclasses import dataclass

import psycopg2

from .. import app


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


def run_query(database_url: str, sql: str) -> QueryResult:
    # In read-only apps (explore-db) the server itself rejects any write: a
    # read-only transaction fails on INSERT/UPDATE/DELETE/DDL, so the guarantee
    # does not depend on parsing the SQL. Committing a read-only transaction is
    # harmless, so the surrounding flow stays identical for both apps.
    connect_kwargs = {"sslmode": "require"}
    if app.current().read_only:
        connect_kwargs["options"] = "-c default_transaction_read_only=on"
    conn = psycopg2.connect(database_url, **connect_kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)

            if cur.description is not None:
                columns = [desc[0] for desc in cur.description]
                result = QueryResult("rows", columns=columns, rows=cur.fetchall())
            elif cur.rowcount >= 0:
                result = QueryResult("count", rowcount=cur.rowcount)
            else:
                # rowcount is -1 when undefined (e.g. DDL such as CREATE/ALTER).
                result = QueryResult("ok")

        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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

    Both conditions below are load-bearing even though measured psycopg2 errors
    happen to satisfy them together: `pgcode` is the documented "the server
    answered" signal, and an empty `diag` must never render as "None".

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
