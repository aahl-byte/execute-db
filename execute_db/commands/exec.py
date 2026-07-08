"""The default (no-subcommand) command: run SQL against an environment.

Builds the env-flag parser, resolves the connection URL (from an environment or
an ephemeral token), reads the SQL, executes it via `core.query`, and formats
the result for the terminal.
"""

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from .flags import add_env_flags, selected_env
from ..console import fail
from ..core import query, store, tokens
from ..core.store import discover_envs
from ..core.system import in_system_mode


def build_parser(envs: list) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execute-db",
        description=(
            "Execute SQL statements against configured databases.\n\n"
            "Statements run in a single transaction: committed on success, rolled\n"
            "back on error. Each environment is a .env.<name> file in the store;\n"
            "each becomes an --<name> flag. Password-protected environments\n"
            "prompt for their password on the terminal."
        ),
        epilog='examples:\n'
               '  execute-db --dev "INSERT INTO users (name) VALUES (\'Alice\')"\n'
               '  execute-db --dev -f migration.sql\n'
               '  execute-db --dev < migration.sql\n'
               '  execute-db --token 8YOfCttjVdI5FdUfB-X6Vw "SELECT 1"\n'
               '\n'
               'management commands (details: execute-db <command> --help):\n'
               '  password set --<env>            encrypt an env file with a password\n'
               '  password change --<env>         rotate an env file\'s password\n'
               '  token create --<env> --ttl 2h   mint a short-lived password-free token\n'
               '  token list                      show active tokens\n'
               '  token revoke <id>               revoke a token early',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN",
                       help="connect with an ephemeral access token instead of an "
                            "environment (no password prompt; see `execute-db token --help`)")
    parser.add_argument("sql", nargs="?",
                        help="SQL statement to execute (omit to read from -f FILE or stdin)")
    parser.add_argument("-f", "--file", metavar="FILE",
                        help="read the SQL to execute from a .sql file")
    parser.add_argument("-o", "--format", choices=FORMATS, default="table",
                        help="output format for result rows (default: table)")
    parser.add_argument("--meta", action="store_true",
                        help="print a row-count/columns summary to stderr")
    return parser


FORMATS = ("table", "json", "jsonl", "csv", "list")


def _cell(value) -> str:
    """Render one cell for the text formats (table/csv/list).

    NULL is shown literally so it is distinguishable from an empty string;
    dicts/lists (jsonb, arrays) are JSON-encoded so nested data round-trips;
    everything else (datetimes, numbers, ...) coerces via str().
    """
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _format_table(columns: list, rows: list) -> str:
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def line(values):
        return " | ".join(v.ljust(widths[i]) for i, v in enumerate(values))

    out = [line(columns), "-+-".join("-" * w for w in widths)]
    out += [line(row) for row in cells]
    return "\n".join(out)


def format_result(result: query.QueryResult, fmt: str) -> str:
    """Format a result's *data* for stdout. Non-row kinds carry no data ("")."""
    if result.kind != "rows":
        return ""

    columns, rows = result.columns, result.rows
    if fmt == "json":
        objs = [dict(zip(columns, row)) for row in rows]
        return json.dumps(objs, indent=2, default=str)
    if fmt == "jsonl":
        return "\n".join(
            json.dumps(dict(zip(columns, row)), default=str) for row in rows
        )
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([_cell(v) for v in row] for row in rows)
        return buf.getvalue().rstrip("\n")
    if fmt == "list":
        return "\n".join("\t".join(_cell(v) for v in row) for row in rows)
    return _format_table(columns, rows)


def _print_result(result: query.QueryResult, fmt: str = "table", meta: bool = False):
    # stdout carries result data only; status/metadata go to stderr so piped
    # output (csv/json/...) stays clean.
    if result.kind == "rows":
        data = format_result(result, fmt)
        if data:
            print(data)
        if meta:
            n = len(result.rows)
            print(f"{n} row{'' if n == 1 else 's'}, "
                  f"columns: {', '.join(result.columns)}", file=sys.stderr)
    elif result.kind == "count":
        print(f"Rows affected: {result.rowcount}", file=sys.stderr)
    else:
        print("Statement executed.", file=sys.stderr)


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             "`execute-db config set <name>`.")
    parser = build_parser(envs)
    args = parser.parse_args(argv)

    if args.token:
        database_url = tokens.load_database_url_from_token(args.token)
    else:
        env = selected_env(args, envs)
        database_url = store.load_database_url(env)

    if args.file:
        # The trusted launcher converts -f into piped stdin *as the calling
        # user*; if -f still reaches the service-user process, it would open the
        # path as the service user (a file-read primitive). Refuse it here.
        if in_system_mode():
            fail("-f/--file is not available in hardened (system) mode; "
                 "pipe the SQL via stdin instead")
        sql = Path(args.file).read_text()
    elif args.sql:
        sql = args.sql
    elif not sys.stdin.isatty():
        sql = sys.stdin.read()
    else:
        parser.error("provide SQL as an argument, via -f FILE, or pipe to stdin")

    try:
        _print_result(query.run_query(database_url, sql), args.format, args.meta)
    except Exception as e:
        # In system mode the agent sees this over sudo; psycopg2 errors can echo
        # host/user/dbname. Keep the detail for interactive user-mode debugging.
        if in_system_mode():
            fail("Query failed")
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
