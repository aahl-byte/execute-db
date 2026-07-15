"""The `schema` command: emit a database's full schema as JSON, cached.

Built for an external tool (auto-complete, linting, option hints, UI search)
that loads the document once per refresh and re-indexes it into its own
structure — so the whole document is served, always. There are no projection
flags: the consumer re-indexes anyway, so slicing here would only hand it a
subset to work around.

Adds NO disclosure surface: anyone who can run a query here can already read
`pg_catalog` directly. This is a convenience wrapper over statements the caller
is already authorized to run.
"""

import argparse
import sys

from .flags import add_env_flags, selected_env
from .. import app
from ..console import fail
from ..core import query, schema, store, tokens
from ..core.store import discover_envs
from ..core.system import in_system_mode


def build_parser(envs: list) -> argparse.ArgumentParser:
    # Deliberately no -o/--format and no pager, unlike the exec path. Every
    # renderer there is row-shaped and none of them fits a nested document, and
    # the consumer wants JSON — a format flag would only offer worse answers to
    # a question nobody has. A pager would be worse still: this is megabytes of
    # machine input, so paging it means an interactive prompt in front of a tool.
    name = app.current().name
    parser = argparse.ArgumentParser(
        prog=f"{name} schema",
        description=(
            "Print a complete JSON description of an environment's schema:\n"
            "tables, views, columns, constraints, indexes, enums, domains,\n"
            "functions, sequences, triggers, and comments.\n\n"
            "The whole document is always printed — it is meant to be read once\n"
            "and indexed by whatever consumes it. Only the JSON goes to stdout,\n"
            "so it pipes straight into a parser or redirects into a file.\n\n"
            "The result is cached, so repeated calls do not re-introspect."
        ),
        epilog="examples:\n"
               f"  {name} schema --dev > schema.json\n"
               f"  {name} schema --dev | python -m json.tool\n"
               f"  {name} schema --dev --refresh          # after a migration\n"
               f"  {name} schema --dev --max-age 1h       # accept an older cache\n"
               f"  {name} schema --token <TOKEN> --meta   # unattended, with status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN",
                       help="use an ephemeral access token instead of an "
                            f"environment (see `{name} token --help`)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-introspect now, ignoring any cached copy")
    parser.add_argument("--max-age", metavar="AGE", default=None,
                        help="serve a cached copy only if younger than AGE "
                             "(45s/30m/2h/1d; default "
                             f"{schema.DEFAULT_MAX_AGE_SECONDS // 60}m, 0 to bypass)")
    parser.add_argument("--meta", action="store_true",
                        help="report cache status (cached/refreshed) on stderr")
    return parser


def parse_max_age(text: "str | None") -> float:
    """`--max-age` in seconds. Bare `0` means "bypass the cache".

    Lives here rather than in core.schema because it is an argparse concern:
    `load` takes a number of seconds, and "30m" is a spelling the terminal uses.
    The core layer has to stay callable with a plain float by anything that is
    not a command line.

    Built on `tokens.parse_duration`, not `tokens.parse_ttl` — see the former's
    docstring for why a cache lifetime does not want the latter's rules.

    `0` is special-cased because the grammar requires a unit and zero has none
    that means anything different. `0s` also works, straight out of the grammar;
    `0` is simply what people type, and what --help documents.
    """
    if text is None:
        return schema.DEFAULT_MAX_AGE_SECONDS
    if text == "0":
        return 0
    return tokens.parse_duration(text, "--max-age")


def _age_text(seconds: "float | None") -> str:
    """A coarse age for --meta, in the units --max-age is spelled in.

    None is a real answer, not a gap: `load` re-stats the file to age a cache
    hit, and an entry cleared in between leaves the age unknown while the
    document it already read is still perfectly good (see core.schema.load).
    Handled here so the one caller has one branch, and so a None can never be
    formatted into "age None".
    """
    if seconds is None:
        return "unknown"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             f"`{app.current().name} config set <name>`.")
    parser = build_parser(envs)
    args = parser.parse_args(argv)

    # Before the URL, not inline at the load() call below: resolving an
    # encrypted environment prompts for its password, and `schema --prod
    # --max-age soon` asking for the prod password only to then reject the flag
    # is backwards. A malformed flag is decided before anything reaches for a
    # credential.
    max_age = parse_max_age(args.max_age)

    if args.token:
        database_url = tokens.load_database_url_from_token(args.token)
    else:
        database_url = store.load_database_url(selected_env(args, envs))

    try:
        result = schema.load(database_url, max_age=max_age, refresh=args.refresh)
    except Exception as e:
        # The same split as the exec path (see commands/exec.py and
        # query.server_error): over sudo the caller may be an agent, and a
        # psycopg2 CONNECTION error can echo host/user/dbname — so that stays
        # withheld. A SERVER-side error (one with a SQLSTATE) only ever
        # describes the caller's own statement, and withholding that would leave
        # them with "it failed" and nowhere to go.
        #
        # Only load() is inside the try. Resolving the URL above fails on its
        # own terms — a bad password is not an introspection failure — and
        # sweeping it in here would relabel those messages as this one, or in
        # hardened mode reduce them to the bare string.
        if in_system_mode():
            detail = query.server_error(e)
            fail(f"Schema introspection failed: {detail}" if detail
                 else "Schema introspection failed")
        print(f"Schema introspection failed: {e}", file=sys.stderr)
        sys.exit(1)

    # A byte copy through the binary buffer, never print(): the document is
    # megabytes of exactly what Postgres produced, and going through the text
    # layer to re-encode it would cost time and give nothing back. The trailing
    # newline is ours — jsonb::text emits none, so `schema --dev > schema.json`
    # would otherwise leave a file that does not end in one. Every JSON parser
    # ignores it.
    sys.stdout.buffer.write(result.document)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()

    # `is False` means a write was ATTEMPTED and failed. A cache hit leaves this
    # None -- nothing was attempted -- and warning there would fire on the
    # common path, which is the surest way to train someone to ignore a warning
    # that only ever matters because it is rare.
    if result.cache_written is False:
        print("Warning: the schema could not be cached; the next call will "
              "introspect again.", file=sys.stderr)
    if args.meta:
        # elapsed is the introspection, not this process end to end: the wait is
        # the database's, and the cache write is milliseconds against seconds.
        print(f"cached (age {_age_text(result.age)})" if result.cached
              else f"refreshed in {result.elapsed:.1f}s", file=sys.stderr)
