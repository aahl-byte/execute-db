"""Command-line entry points and argument parsing.

This module wires argparse to the domain modules; the actual work lives in
`envs`, `tokens`, `passwords`, `config_cmd`, and `query`. Names re-exported
below (discover_envs, env_file_path, the cmd_* handlers, …) are the tested
surface — importing them here keeps `from execute_db import cli` working.
"""

import argparse
import sys
from pathlib import Path

from . import __version__, crypto, kernel_keyring, paths, system
from .config_cmd import (
    cmd_config_list, cmd_config_rm, cmd_config_set, config_main,
    prompt_confirm, read_connection_url, redact_url,
)
from .envs import (
    discover_envs, env_flag_help, load_database_url, read_env_text,
    require_encrypted, url_from_env_text, write_encrypted,
)
from .passwords import cmd_password_change, cmd_password_set
from .paths import env_file_path, validate_alias
from .query import run_query
from .system import in_system_mode, maybe_redirect_to_launcher
from .tokens import (
    cmd_token_create, cmd_token_list, cmd_token_revoke,
    load_database_url_from_token, revoke_all_tokens, sweep_expired_tokens,
)
from .util import fail


def env_dest(env: str) -> str:
    return "env_" + env.replace("-", "_")


def selected_env(args, envs: list) -> str:
    return next((e for e in envs if getattr(args, env_dest(e))), None)


def add_env_flags(parser: argparse.ArgumentParser, envs: list,
                  required: bool = True):
    group = parser.add_mutually_exclusive_group(required=required)
    for env in envs:
        group.add_argument(
            f"--{env}", dest=env_dest(env), action="store_true",
            help=env_flag_help(env),
        )
    return group


def manage_main():
    """Handle the `password` and `token` management subcommands."""
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             "`execute-db config set <name>`.")

    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="execute-db",
        description="Manage access to execute-db environments.",
        epilog='examples:\n'
               '  execute-db password set --dev\n'
               '  execute-db password change --dev\n'
               '  execute-db token create --dev --ttl 2h\n'
               '  execute-db token list\n'
               '  execute-db token revoke 8df8dbeb3696',
        formatter_class=raw,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{password,token}")

    p_password = sub.add_parser(
        "password",
        help="encrypt env files with a password / rotate passwords",
        description=(
            "Encrypt an environment's .env file so it can only be used after\n"
            "entering its password on an interactive terminal.\n\n"
            "Files are encrypted with AES-256-GCM (scrypt-derived key). There is\n"
            "no password recovery: if you forget it, delete the encrypted file,\n"
            "recreate it with your connection string, and set a password again."
        ),
        formatter_class=raw,
    )
    pw_sub = p_password.add_subparsers(dest="action", required=True, metavar="{set,change}")
    p_set = pw_sub.add_parser(
        "set",
        help="encrypt a plaintext .env file with a new password",
        description=(
            "Encrypt an environment's plaintext .env file. Prompts for a new\n"
            "password (twice) on the terminal, encrypts the file, and makes a\n"
            "best-effort wipe of the plaintext original.\n\n"
            "Afterwards, running SQL against the environment prompts for the\n"
            "password; non-interactive callers are refused (use an ephemeral\n"
            "token for that — see `execute-db token create --help`)."
        ),
        formatter_class=raw,
    )
    add_env_flags(p_set, envs)
    p_change = pw_sub.add_parser(
        "change",
        help="change the password of an encrypted .env file",
        description=(
            "Rotate an environment's password: prompts for the current password,\n"
            "then a new one (twice). The decrypted contents never touch disk."
        ),
        formatter_class=raw,
    )
    add_env_flags(p_change, envs)

    p_token = sub.add_parser(
        "token",
        help="create/list/revoke short-lived password-free access tokens",
        description=(
            "Ephemeral tokens grant temporary, password-free access to one\n"
            "environment — e.g. handing a script or coding agent scoped access\n"
            "for an afternoon. A token works without a terminal until it expires\n"
            "or is revoked."
        ),
        formatter_class=raw,
    )
    tok_sub = p_token.add_subparsers(dest="action", required=True, metavar="{create,list,revoke}")
    p_create = tok_sub.add_parser(
        "create",
        help="create a short-lived token for an environment",
        description=(
            "Create a token for one environment. If the environment is password\n"
            "protected you are prompted for its password — the token is a copy of\n"
            "the credentials re-encrypted under a fresh random secret with the\n"
            "expiry sealed into the authenticated header.\n\n"
            "Half of the encryption key (a key share) lives only in the kernel\n"
            "keyring with a TTL: the kernel destroys it at expiry or reboot, so\n"
            "even a copied token file becomes permanently undecryptable.\n\n"
            "The token is printed ONCE and cannot be recovered; pass it to the\n"
            'holder, who runs:  execute-db --token <TOKEN> "SELECT ..."'
        ),
        formatter_class=raw,
    )
    add_env_flags(p_create, envs)
    p_create.add_argument("--ttl", required=True, metavar="DURATION",
                          help="token lifetime: <n>s|m|h|d, e.g. 45s, 30m, 2h, 1d")
    tok_sub.add_parser(
        "list",
        help="list active tokens (purges expired ones)",
        description=(
            "List active token ids and their expiry times. Token files that have\n"
            "already expired are deleted as a side effect. The token secrets\n"
            "themselves are never shown — they are only displayed at creation."
        ),
        formatter_class=raw,
    )
    p_revoke = tok_sub.add_parser(
        "revoke",
        help="revoke a token by id, before it expires",
        description="Delete a token so it stops working immediately.",
    )
    p_revoke.add_argument("id", help="token id, as shown by `execute-db token list`")
    tok_sub.add_parser(
        "sweep",
        help="wipe expired token files now",
        description=(
            "Wipe any expired token files. Runs automatically via systemd user\n"
            "timers (scheduled at each token's expiry, plus once after boot) and\n"
            "as a backstop on every execute-db invocation, so you rarely need to\n"
            "run it by hand."
        ),
        formatter_class=raw,
    )

    args = parser.parse_args()

    try:
        if args.command == "password":
            env = selected_env(args, envs)
            if args.action == "set":
                cmd_password_set(env)
            else:
                cmd_password_change(env)
        elif args.command == "token":
            if args.action == "create":
                cmd_token_create(selected_env(args, envs), args.ttl)
            elif args.action == "list":
                cmd_token_list()
            elif args.action == "sweep":
                sweep_expired_tokens(verbose=True)
            else:
                cmd_token_revoke(args.id)
    except crypto.NoTTYError:
        fail("This command needs an interactive terminal to prompt for a password.")
    except crypto.CryptoError as e:
        fail(str(e))


def exec_main():
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

    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             "`execute-db config set <name>`.")
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN",
                       help="connect with an ephemeral access token instead of an "
                            "environment (no password prompt; see `execute-db token --help`)")

    parser.add_argument("sql", nargs="?",
                        help="SQL statement to execute (omit to read from -f FILE or stdin)")
    parser.add_argument("-f", "--file", metavar="FILE",
                        help="read the SQL to execute from a .sql file")
    args = parser.parse_args()

    if args.token:
        database_url = load_database_url_from_token(args.token)
    else:
        env = selected_env(args, envs)
        database_url = load_database_url(env)

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
        run_query(database_url, sql)
    except Exception as e:
        # In system mode the agent sees this over sudo; psycopg2 errors can echo
        # host/user/dbname. Keep the detail for interactive user-mode debugging.
        if in_system_mode():
            fail("Query failed")
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)


TOP_LEVEL_HELP = """\
execute-db {version} — run SQL against configured PostgreSQL environments

Run SQL:
  execute-db --<env> "SELECT 1"          run a statement against an environment
  execute-db --<env> -f migration.sql    run SQL from a file
  cat q.sql | execute-db --<env>          run SQL piped on stdin
  execute-db --token <TOKEN> "SELECT 1"   run with an ephemeral token

Manage environments (each is an encrypted .env.<name> file in ~/.execute-db):
  execute-db config list                 list environments and their state
  execute-db config set <name>           create/replace one (prompts for URL + password)
  execute-db config rm <name>            remove one and revoke outstanding tokens

Password protection:
  execute-db password set --<env>        encrypt an environment with a password
  execute-db password change --<env>     rotate an environment's password

Ephemeral tokens (temporary password-free access):
  execute-db token create --<env> --ttl 2h   mint a short-lived token
  execute-db token list                       list active tokens
  execute-db token revoke <id>                revoke a token early

  execute-db --version                   print the version
  execute-db <command> --help            detailed help for a command
"""


def print_top_level_help():
    print(TOP_LEVEL_HELP.format(version=__version__))


def main():
    maybe_redirect_to_launcher()

    argv = sys.argv[1:]

    # Top-level help / version: handle before any store access so they work even
    # with no environments configured (exec_main would otherwise error first).
    if not argv or argv[0] in ("-h", "--help"):
        print_top_level_help()
        return
    if argv[0] in ("-V", "--version", "version"):
        print(f"execute-db {__version__}")
        return

    # `config` manages the store in place (and must work with zero envs), so it
    # runs after the launcher redirect but before the env-flag-building paths.
    if argv[0] == "config":
        config_main()
        return

    # Backstop: the systemd timers do the wall-clock wiping, but sweep here too
    # in case they were unavailable. Skip for `token` commands, which sweep for
    # themselves (verbosely). Never let this break the actual command.
    if len(sys.argv) <= 1 or sys.argv[1] != "token":
        try:
            sweep_expired_tokens()
        except Exception:
            pass

    if len(sys.argv) > 1 and sys.argv[1] in ("password", "token"):
        manage_main()
    else:
        exec_main()
