"""The default (no-subcommand) command: run SQL against an environment.

Builds the env-flag parser, resolves the connection URL (from an environment or
an ephemeral token), reads the SQL, executes it via `core.query`, and formats
the result for the terminal.
"""

import argparse
import csv
import io
import json
import os
import shlex
import subprocess
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
            "Run SQL against one of your configured PostgreSQL environments.\n\n"
            "Pick the target with its --<name> flag (run `execute-db config list`\n"
            "to see what's available). Supply the SQL as a quoted argument, from a\n"
            "file with -f, or piped on stdin.\n\n"
            "All statements run inside a single transaction: it commits if every\n"
            "statement succeeds and rolls back entirely on the first error, so a\n"
            "failed migration leaves nothing half-applied. A SELECT prints its\n"
            "rows; a write reports the row count; DDL just confirms it ran.\n\n"
            "Password-protected environments prompt for their password on the\n"
            "terminal; use an ephemeral token (see below) for unattended access."
        ),
        epilog='examples:\n'
               '  execute-db --dev "SELECT * FROM users LIMIT 5"   run a query\n'
               '  execute-db --dev "INSERT INTO users (name) VALUES (\'Alice\')"\n'
               '  execute-db --dev -f migration.sql                run SQL from a file\n'
               '  execute-db --dev < migration.sql                 same, via stdin\n'
               '  execute-db --prod -o csv "TABLE users" > out.csv export to CSV\n'
               '  execute-db --token 8YOfCttjVdI5FdUfB-X6Vw "SELECT 1"\n'
               '\n'
               'output formats (-o/--format):\n'
               '  table     aligned columns, easy to read in a terminal (default)\n'
               '  vertical  one field per line (psql \\x style) — best for wide rows\n'
               '  json      a JSON array of row objects — feed to jq or an app\n'
               '  jsonl     one JSON object per line — streams large result sets\n'
               '  csv       comma-separated with a header row — open in a spreadsheet\n'
               '  list      tab-separated values, no header — for cut/awk/xargs\n'
               '\n'
               '  Only result rows go to stdout; row counts and --meta summaries go\n'
               '  to stderr, so redirecting (e.g. -o csv > out.csv) yields a clean\n'
               '  file. table and vertical are paged through $PAGER (default `less\n'
               '  -S`, so wide rows scroll sideways) at a terminal; --no-pager or\n'
               '  any machine format prints straight through.\n'
               '\n'
               'management commands (details: execute-db <command> --help):\n'
               '  config set <name>               add or replace an environment\n'
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
    parser.add_argument("sql", nargs="?", metavar="SQL",
                        help="the SQL to run, as one quoted argument "
                             "(omit to read from -f FILE or piped stdin)")
    parser.add_argument("-f", "--file", metavar="FILE",
                        help="read the SQL to run from a .sql file instead of an argument")
    parser.add_argument("-o", "--format", choices=FORMATS, default="table",
                        metavar="FORMAT",
                        help="how to render result rows: "
                             "table (default), vertical, json, jsonl, csv, or list "
                             "(see 'output formats' below)")
    parser.add_argument("--meta", action="store_true",
                        help="also print a `N rows, columns: ...` summary to stderr")
    parser.add_argument("--no-pager", dest="pager", action="store_false",
                        help="print table/vertical output straight to stdout instead "
                             "of paging it through $PAGER at a terminal")
    return parser


# `table` and `vertical` are for human eyes: at a TTY they are paged (so wide
# rows scroll instead of wrapping). The rest are machine formats, never paged.
FORMATS = ("table", "vertical", "json", "jsonl", "csv", "list")
HUMAN_FORMATS = ("table", "vertical")


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


def _format_vertical(columns: list, rows: list) -> str:
    """One block per row (psql \\x style): a `column | value` line per field.

    Values are not wrapped; wide ones scroll under the pager rather than
    breaking the alignment.
    """
    if not rows:
        return ""
    label = max(len(c) for c in columns)
    blocks = []
    for i, row in enumerate(rows, 1):
        lines = [f"[ row {i} ]"]
        lines += [f"{col.ljust(label)} | {_cell(val)}"
                  for col, val in zip(columns, row)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
    if fmt == "vertical":
        return _format_vertical(columns, rows)
    return _format_table(columns, rows)


def _run_pager(text: str) -> bool:
    """Display `text` through a pager. Return False if none could run.

    Honors $PAGER if set; otherwise `less -S` (chop long lines so wide rows
    scroll left/right), `-R` (pass color through), `-F` (quit if it fits on
    one screen). A missing pager or an early quit (broken pipe) is not fatal.
    """
    pager = os.environ.get("PAGER")
    # Split $PAGER into argv (so "less -R" works) and run without a shell, so a
    # missing/misspelled pager raises OSError here and we fall back to print
    # rather than the shell swallowing the text and reporting bogus success.
    cmd = shlex.split(pager) if pager else ["less", "-S", "-R", "-F"]
    if not cmd:
        return False  # PAGER set but empty/whitespace
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    except OSError:
        return False  # e.g. pager not installed
    try:
        proc.communicate(text.encode())
    except BrokenPipeError:
        pass
    return True


def _emit(text: str, use_pager: bool):
    """Write result data to stdout, paging it when asked and at a terminal."""
    if use_pager and sys.stdout.isatty() and _run_pager(text):
        return
    print(text)


def _print_result(result: query.QueryResult, fmt: str = "table",
                  meta: bool = False, pager: bool = True):
    # stdout carries result data only; status/metadata go to stderr so piped
    # output (csv/json/...) stays clean.
    if result.kind == "rows":
        data = format_result(result, fmt)
        if data:
            _emit(data, use_pager=pager and fmt in HUMAN_FORMATS)
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
        _print_result(query.run_query(database_url, sql),
                      args.format, args.meta, args.pager)
    except Exception as e:
        # In system mode the agent sees this over sudo; psycopg2 errors can echo
        # host/user/dbname. Keep the detail for interactive user-mode debugging.
        if in_system_mode():
            fail("Query failed")
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
