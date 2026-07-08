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

Quick start:
  execute-db config set dev            add an environment (prompts for URL + password)
  execute-db --dev "SELECT 1"          run SQL against it

Run SQL against an environment (each configured env is an --<env> flag):
  execute-db --dev "SELECT * FROM t"   pass SQL as a quoted argument
  execute-db --dev -f migration.sql    ...or read it from a .sql file
  execute-db --dev < migration.sql     ...or pipe it on stdin
  execute-db --dev -o csv "TABLE t"    pick an output format (see below)
  execute-db --token <TOKEN> "..."     use an ephemeral token instead of --<env>
  Everything runs in one transaction: commit on success, rollback on any error.
  Encrypted envs prompt for a password; use a token for unattended access.

Output formats (-o/--format, default: table):
  table     aligned columns, paged at a terminal (--no-pager to disable)
  vertical  one field per line (psql \\x style) — best for wide rows
  json      pretty JSON array      jsonl  one JSON object per line
  csv       header + rows          list   tab-separated, no header (for cut/awk)
  Only result rows go to stdout; row counts and --meta summaries go to stderr.

Manage environments (each is an encrypted .env.<name> file in ~/.execute-db):
  execute-db config list               list environments and whether each is encrypted
  execute-db config set <name>         create/replace one (prompts for URL + password)
  execute-db config rm <name>          remove one and revoke its tokens

Password-protect an environment:
  execute-db password set --dev        encrypt an environment with a password
  execute-db password change --dev     rotate its password

Ephemeral tokens — temporary, password-free access (e.g. for a script or agent):
  execute-db token create --dev --ttl 2h   mint a short-lived token (45s/30m/2h/1d)
  execute-db token list                     list active tokens
  execute-db token revoke <id>              revoke one early

  execute-db --version                 print the version
  execute-db <command> --help          per-command flags (config/password/token)
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
