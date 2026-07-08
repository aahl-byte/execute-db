"""Command-line entry point: redirect, top-level help/version, and dispatch.

The actual work lives in the `commands` package (argparse + presentation) over
the `core` package (pure logic). This module only routes an invocation to the
right command. It is shared by both front-ends; everything app-specific (the
command name, read-only vs read/write wording, config dir) is read from the
active `AppSpec` — see `db_core.app`. The front-end installs that spec, then
calls `main()`.
"""

import sys

from . import app
from .commands import config, password, token
from .commands import exec as exec_cmd
from .core import tokens
from .core.system import maybe_redirect_to_launcher

TOP_LEVEL_HELP = """\
{name} {version} — run {sql_kind} against configured PostgreSQL environments

Quick start:
  {name} config set dev            add an environment (prompts for URL + optional password)
  {name} --dev "SELECT 1"          run SQL against it

Run SQL against an environment (each configured env is an --<env> flag):
  {name} --dev "SELECT * FROM t"   pass SQL as a quoted argument
  {name} --dev -f query.sql        ...or read it from a .sql file
  {name} --dev < query.sql         ...or pipe it on stdin
  {name} --dev -o csv "TABLE t"    pick an output format (see below)
  {name} --token <TOKEN> "..."     use an ephemeral token instead of --<env>
  {txn_note}
  Encrypted envs prompt for a password; use a token for unattended access.

Output formats (-o/--format, default: table):
  table     aligned columns, paged at a terminal (--no-pager to disable)
  vertical  one field per line (psql \\x style) — best for wide rows
  json      pretty JSON array      jsonl  one JSON object per line
  csv       header + rows          list   tab-separated, no header (for cut/awk)
  Only result rows go to stdout; row counts and --meta summaries go to stderr.

Manage environments (each is a .env.<name> file in ~/{config_dirname}):
  {name} config list               list environments and whether each is encrypted
  {name} config set <name>         create/replace one (prompts for URL + optional password)
  {name} config rm <name>          remove one and revoke its tokens

Password-protect an environment (optional — envs may be plaintext):
  {name} password set --dev        encrypt an environment with a password
  {name} password change --dev     rotate its password

Ephemeral tokens — temporary, password-free access (e.g. for a script or agent):
  {name} token create --dev --ttl 2h   mint a short-lived token (45s/30m/2h/1d)
  {name} token list                     list active tokens
  {name} token revoke <id>              revoke one early

  {name} --version                 print the version
  {name} <command> --help          per-command flags (config/password/token)
"""


def _help_fields() -> dict:
    spec = app.current()
    if spec.read_only:
        sql_kind = "read-only SQL"
        txn_note = "Runs in one read-only transaction — the server rejects any write."
    else:
        sql_kind = "SQL"
        txn_note = "Everything runs in one transaction: commit on success, rollback on any error."
    return {
        "name": spec.name,
        "version": spec.version,
        "config_dirname": spec.config_dirname,
        "sql_kind": sql_kind,
        "txn_note": txn_note,
    }


def print_top_level_help():
    print(TOP_LEVEL_HELP.format(**_help_fields()))


def main():
    maybe_redirect_to_launcher()

    spec = app.current()
    argv = sys.argv[1:]

    # Top-level help / version: handle before any store access so they work even
    # with no environments configured (the exec path would otherwise error first).
    if not argv or argv[0] in ("-h", "--help"):
        print_top_level_help()
        return
    if argv[0] in ("-V", "--version", "version"):
        print(f"{spec.name} {spec.version}")
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
