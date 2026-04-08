import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

CONFIG_DIR = Path.home() / ".explore-db"
CONFIG_FILE = CONFIG_DIR / "config.json"

ENVIRONMENTS = ["dev", "staging", "production"]


def init_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    CONFIG_FILE.write_text(json.dumps(
        {e: f".env.{e}" for e in ENVIRONMENTS}, indent=2,
    ) + "\n")

    for env in ENVIRONMENTS:
        env_file = CONFIG_DIR / f".env.{env}"
        env_file.write_text(f"DATABASE_URL=postgresql://user:password@host:5432/dbname\n")

    print(f"Created default config at: {CONFIG_DIR}")
    print(f"Update your connection strings before running queries:")
    print(f"  {CONFIG_FILE}")
    for env in ENVIRONMENTS:
        print(f"  {CONFIG_DIR / f'.env.{env}'}")
    sys.exit(0)


def load_database_url(env: str) -> str:
    if not CONFIG_FILE.exists():
        init_config()

    config = json.loads(CONFIG_FILE.read_text())

    if env not in config:
        print(f"Environment '{env}' not found in {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)

    entry = config[env]

    # Support direct URL string or .env filename
    if entry.startswith("postgresql://") or entry.startswith("postgres://"):
        return entry

    env_path = CONFIG_DIR / entry
    if not env_path.exists():
        print(f"Env file not found: {env_path}", file=sys.stderr)
        sys.exit(1)

    values = dotenv_values(env_path)
    url = values.get("DATABASE_URL")
    if not url:
        print(f"DATABASE_URL not set in {env_path}", file=sys.stderr)
        sys.exit(1)

    return url


def run_query(database_url: str, sql: str):
    conn = psycopg2.connect(
        database_url,
        sslmode="require",
        options="-c default_transaction_read_only=on",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()

            print(f"Columns: {columns}")
            print(f"Row count: {len(rows)}")
            result = [dict(zip(columns, row)) for row in rows]
            print(json.dumps(result, indent=2, default=str))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog="explore-db",
        description="Run read-only SQL queries against configured databases.",
        epilog='examples:\n'
               '  explore-db --dev "SELECT 1"\n'
               '  explore-db --dev -f query.sql\n'
               '  explore-db --dev < query.sql',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    env_group = parser.add_mutually_exclusive_group(required=True)
    for env in ENVIRONMENTS:
        env_group.add_argument(f"--{env}", action="store_true", help=f"connect to {env}")

    parser.add_argument("sql", nargs="?", help="SQL query string to execute")
    parser.add_argument("-f", "--file", help="path to a .sql file to execute")
    args = parser.parse_args()

    env = next(e for e in ENVIRONMENTS if getattr(args, e))
    database_url = load_database_url(env)

    if args.file:
        sql = Path(args.file).read_text()
    elif args.sql:
        sql = args.sql
    elif not sys.stdin.isatty():
        sql = sys.stdin.read()
    else:
        parser.error("provide SQL as an argument, via -f FILE, or pipe to stdin")

    try:
        run_query(database_url, sql)
    except Exception as e:
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
