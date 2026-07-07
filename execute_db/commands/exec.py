"""The default (no-subcommand) command: run SQL against an environment.

Builds the env-flag parser, resolves the connection URL (from an environment or
an ephemeral token), reads the SQL, executes it via `core.query`, and formats
the result for the terminal.
"""

import argparse
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
    return parser


def _print_result(result: query.QueryResult):
    if result.kind == "rows":
        print(f"Columns: {result.columns}")
        print(f"Row count: {len(result.rows)}")
        rows = [dict(zip(result.columns, row)) for row in result.rows]
        print(json.dumps(rows, indent=2, default=str))
    elif result.kind == "count":
        print(f"Rows affected: {result.rowcount}")
    else:
        print("Statement executed.")


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
        _print_result(query.run_query(database_url, sql))
    except Exception as e:
        # In system mode the agent sees this over sudo; psycopg2 errors can echo
        # host/user/dbname. Keep the detail for interactive user-mode debugging.
        if in_system_mode():
            fail("Query failed")
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
