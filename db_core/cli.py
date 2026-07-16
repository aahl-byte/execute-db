"""Command-line entry point: redirect, top-level help/version, and dispatch.

The actual work lives in the `commands` package (argparse + presentation) over
the `core` package (pure logic). This module only routes an invocation to the
right command. It is shared by both front-ends; everything app-specific (the
command name, read-only vs read/write wording, config dir) is read from the
active `AppSpec` — see `db_core.app`. The front-end installs that spec, then
calls `main()`.
"""

import os
import sys

from . import app
from .commands import config, password, token
from .commands import exec as exec_cmd
from .commands import schema as schema_cmd
from .core import schema as schema_core
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

Inspect the schema (cached for {schema_max_age}; --refresh to re-read after a migration):
  {name} schema --dev              dump the whole schema as JSON (for tools)
  {name} schema list --dev         browse: schemas, with table/view/function counts
  {name} schema list public --dev  browse: the tables, views, and functions in a schema
  {name} schema show public.users --dev   browse: one table/view/enum/function in full
  {name} schema find email --dev   browse: search names across the whole database

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
  {name} <command> --help          per-command flags (config/password/token/schema)
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
        # Interpolated, not retyped: `{name} schema --help` reads the same
        # constant, and two copies of a default only stay equal until one moves.
        "schema_max_age": f"{schema_core.DEFAULT_MAX_AGE_SECONDS // 60}m",
    }


def print_top_level_help():
    print(TOP_LEVEL_HELP.format(**_help_fields()))


def _silence_stdout_at_exit():
    """Point stdout at /dev/null so the interpreter-exit flush cannot re-fire.

    Below the 8KB BufferedWriter a failed write is still BUFFERED: `write()` only
    buffers, the explicit `flush()` raises, and the bytes REMAIN — so the
    interpreter's own exit-time flush of the TextIOWrapper hits the same error a
    second time. That prints `Exception ignored in: <_io.TextIOWrapper ...>`
    AFTER this handler already reported the failure, and replaces our exit 1
    with 120. Redirecting the fd turns that second flush into a successful write
    to nowhere. Above 8KB the raw write raises with nothing retained, the exit
    flush never fires, and this is a harmless no-op. Both sizes are measured and
    pinned by tests/test_cli.py::test_a_failed_write_leaves_no_shutdown_noise.
    """
    # This runs while reporting another error and must never raise over it:
    # `fileno()` gives io.UnsupportedOperation (an OSError) when stdout is not a
    # real fd, and a plain ValueError when it is already closed.
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def main():
    """Route one invocation, mapping a failed stdout write to one line + exit 1.

    The handler lives here, not in the front-ends (`execute_db/cli.py`,
    `explore_db/cli.py`): those are two thin copies of the same three lines, so a
    handler there would be duplicated and free to drift, and every command
    dispatched below inherits this one for nothing.
    """
    try:
        _run()
    except OSError as e:
        # Two ordinary things land here, both from the emit in
        # commands/schema.py that sits outside that command's try on purpose (it
        # reports database-disclosure errors; a failed write is not one):
        # `schema --dev > schema.json` onto a full disk, and `schema --dev |
        # head` closing the pipe. BrokenPipeError is an OSError, so one handler
        # covers both, and a traceback here would print the service user's
        # install path in hardened mode.
        #
        # The message is GENERIC and does NOT name the write: this catch is also
        # broad enough to see a store OSError (an unreadable .env.dev), and
        # calling that "could not write to stdout" would be exactly the
        # mislabelling that test_schema.py's store/introspection boundary test
        # exists to prevent, relocated one layer up.
        print(f"{app.current().name}: {e}", file=sys.stderr)
        _silence_stdout_at_exit()
        sys.exit(1)


def _run():
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
    elif argv[0] == "schema":
        schema_cmd.run(argv[1:])
    else:
        exec_cmd.run(argv)
