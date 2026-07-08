"""Command-line entry point: redirect, top-level help/version, and dispatch.

The actual work lives in the `commands` package (argparse + presentation) over
the `core` package (pure logic). This module only routes an invocation to the
right command.
"""

import sys

from . import __version__
from .commands import config, password, token
from .commands import exec as exec_cmd
from .core import tokens
from .core.system import maybe_redirect_to_launcher

TOP_LEVEL_HELP = """\
execute-db {version} — run SQL against configured PostgreSQL environments

New here? Start by adding an environment, then run SQL against it:
  execute-db config set dev              save a connection (prompts for URL + password)
  execute-db --dev "SELECT 1"            run a statement against it

Run SQL:
  execute-db --<env> "SELECT 1"          run a statement against an environment
  execute-db --<env> -f migration.sql    run SQL from a file
  cat q.sql | execute-db --<env>          run SQL piped on stdin
  execute-db --<env> -o csv "TABLE t"    choose an output format (table/json/csv/...)
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

More help:
  execute-db --<env> --help              full options + output formats for running SQL
  execute-db <command> --help            detailed help for config/password/token
  execute-db --version                   print the version
"""


def print_top_level_help():
    print(TOP_LEVEL_HELP.format(version=__version__))


def main():
    maybe_redirect_to_launcher()

    argv = sys.argv[1:]

    # Top-level help / version: handle before any store access so they work even
    # with no environments configured (the exec path would otherwise error first).
    if not argv or argv[0] in ("-h", "--help"):
        print_top_level_help()
        return
    if argv[0] in ("-V", "--version", "version"):
        print(f"execute-db {__version__}")
        return

    # `config` manages the store in place (and must work with zero envs), so it
    # runs after the launcher redirect but before the env-flag-building paths.
    if argv[0] == "config":
        config.run(argv[1:])
        return

    # Backstop: the systemd timers do the wall-clock wiping, but sweep here too
    # in case they were unavailable. Skip for `token` commands, which sweep for
    # themselves (verbosely). Never let this break the actual command.
    if argv[0] != "token":
        try:
            tokens.sweep_expired()
        except Exception:
            pass

    if argv[0] == "password":
        password.run(argv[1:])
    elif argv[0] == "token":
        token.run(argv[1:])
    else:
        exec_cmd.run(argv)
