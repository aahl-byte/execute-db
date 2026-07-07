"""Run SQL against a database URL in a single transaction."""

import json

import psycopg2


def run_query(database_url: str, sql: str):
    conn = psycopg2.connect(database_url, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)

            if cur.description is not None:
                # Statement returned a result set (SELECT, or ... RETURNING).
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                print(f"Columns: {columns}")
                print(f"Row count: {len(rows)}")
                result = [dict(zip(columns, row)) for row in rows]
                print(json.dumps(result, indent=2, default=str))
            elif cur.rowcount >= 0:
                # Write with no result set (INSERT/UPDATE/DELETE).
                print(f"Rows affected: {cur.rowcount}")
            else:
                # rowcount is -1 when undefined (e.g. DDL such as CREATE/ALTER).
                print("Statement executed.")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
