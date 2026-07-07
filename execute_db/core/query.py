"""Run SQL against a database URL in a single transaction.

Pure logic: execute, commit/rollback, and return a description of what
happened. Formatting the result for the terminal is the command layer's job.
"""

from dataclasses import dataclass

import psycopg2


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
    conn = psycopg2.connect(database_url, sslmode="require")
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
