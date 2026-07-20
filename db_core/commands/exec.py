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
from .. import app
from ..console import fail
from ..core import query, store, tokens
from ..core.split import split_statements
from ..core.store import discover_envs
from ..core.system import in_system_mode


def build_parser(envs: list) -> argparse.ArgumentParser:
    name = app.current().name
    if app.current().read_only:
        sql_kind = "read-only SQL"
        txn_line = ("Everything runs in a single read-only transaction — the server\n"
                    "rejects any write (INSERT/UPDATE/DELETE/DDL).")
    else:
        sql_kind = "SQL"
        txn_line = ("Everything runs in a single transaction (commit on success,\n"
                    "rollback on any error).")
    parser = argparse.ArgumentParser(
        prog=name,
        description=(
            f"Run {sql_kind} against one of your configured PostgreSQL environments.\n\n"
            "Pick the target with its --<name> flag, and supply the SQL as a quoted\n"
            f"argument, from a file with -f, or piped on stdin. {txn_line}\n\n"
            f"Run `{name} --help` for the full overview, including output formats\n"
            "and the config/password/token commands."
        ),
        epilog='examples:\n'
               f'  {name} --dev "SELECT * FROM users LIMIT 5"\n'
               f'  {name} --dev -f query.sql                run SQL from a file\n'
               f'  {name} --dev < query.sql                 same, via stdin\n'
               f'  {name} --prod -o csv "TABLE users" > out.csv export to CSV\n'
               f'  {name} --token <TOKEN> "SELECT 1"            use an ephemeral token',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN",
                       help="connect with an ephemeral access token instead of an "
                            f"environment (no password prompt; see `{name} token --help`)")
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
    parser.add_argument("--multi", action="store_true",
                        help="split the SQL into its statements and show every "
                             "statement's result (same single transaction; "
                             "-o csv/list are not supported — use json or jsonl)")
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


def _statement_obj(r: query.StatementResult) -> dict:
    """One statement as a JSON-able object for the --multi envelope."""
    obj = {"statement": r.index, "preview": r.preview, "kind": r.result.kind}
    if r.result.kind == "rows":
        obj["columns"] = r.result.columns
        obj["rows"] = [dict(zip(r.result.columns, row)) for row in r.result.rows]
    elif r.result.kind == "count":
        obj["rowcount"] = r.result.rowcount
    return obj


def _print_multi(results: list, fmt: str = "table",
                 meta: bool = False, pager: bool = True):
    """Render every statement's result (--multi).

    json/jsonl put EVERYTHING (including count/ok statements) on stdout so a
    machine consumer never has to parse stderr; the shape follows the flag,
    not the statement count. Human formats keep the stdout=data/stderr=status
    split: row sets as headed blocks, everything else as numbered stderr lines.
    """
    if fmt == "json":
        print(json.dumps([_statement_obj(r) for r in results],
                         indent=2, default=str))
        return
    if fmt == "jsonl":
        for r in results:
            print(json.dumps(_statement_obj(r), default=str))
        return

    blocks = []
    for r in results:
        q = r.result
        if q.kind == "rows":
            data = format_result(q, fmt)
            block = f"-- statement {r.index} --"
            if data:
                block += "\n" + data
            blocks.append(block)
            if meta:
                n = len(q.rows)
                print(f"[{r.index}] {n} row{'' if n == 1 else 's'}, "
                      f"columns: {', '.join(q.columns)}", file=sys.stderr)
        elif q.kind == "count":
            print(f"[{r.index}] Rows affected: {q.rowcount}", file=sys.stderr)
        else:
            print(f"[{r.index}] Statement executed.", file=sys.stderr)
    if blocks:
        _emit("\n\n".join(blocks), use_pager=pager and fmt in HUMAN_FORMATS)


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             f"`{app.current().name} config set <name>`.")
    parser = build_parser(envs)
    args = parser.parse_args(argv)

    if args.multi and args.format in ("csv", "list"):
        parser.error(f"--multi cannot render multiple result sets as "
                     f"{args.format}; use -o json or -o jsonl (or drop --multi "
                     "for the last statement's result only)")

    # Resolving the URL stays OUTSIDE the try below: a store failure must fail
    # on its own terms, not get relabelled "Query failed" -- and in system mode
    # that label is all the caller would get. Most store errors exit via fail()
    # -> SystemExit, which `except Exception` never catches anyway, so the
    # boundary only bites for the paths that skip fail() (read_bytes raising
    # OSError, say). commands/schema.py makes the same split for the same reason.
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

    # Counting is not splitting: without --multi the ORIGINAL string still goes
    # to a single execute(), so the lexer can hint but never corrupt.
    n_statements = len(split_statements(sql))

    try:
        if args.multi:
            _print_multi(query.run_multi(database_url, sql),
                         args.format, args.meta, args.pager)
        else:
            _print_result(query.run_query(database_url, sql),
                          args.format, args.meta, args.pager)
            if n_statements > 1:
                print(f"note: {n_statements} statements ran in one transaction; "
                      "only the last result is shown.\n"
                      "      Re-run with --multi to see each statement's result.",
                      file=sys.stderr)
    except Exception as e:
        # Over sudo the caller may be an agent, and a psycopg2 CONNECTION error
        # can echo host/user/dbname — so that detail stays withheld. But a
        # SERVER-side error (one with a SQLSTATE) only ever describes the
        # caller's own SQL, and withholding *that* made the tool unusable for
        # the one thing it exists to do: fix your query. See query.server_error
        # for exactly where the line falls. A StatementError's own text names
        # only a position in the caller's input, so it survives both modes.
        position = f": {e}" if isinstance(e, query.StatementError) else ""
        if in_system_mode():
            detail = query.server_error(e)
            fail(f"Query failed{position}: {detail}" if detail
                 else f"Query failed{position}")
        if position:
            cause = e.__cause__ or e.__context__
            detail = f": {cause}" if cause else ""
            print(f"Query failed{position}{detail}", file=sys.stderr)
        else:
            print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
