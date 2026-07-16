"""The `schema` command: dump a database's schema as JSON, or browse it.

Bare `schema --dev` prints the whole document as JSON, built for an external
tool (auto-complete, linting, option hints, UI search) that loads it once and
re-indexes it — so the whole document is served, always, with no projection.

The `list`/`show`/`find` subcommands are the human side of the same cache: an
easy way to eyeball what schemas, tables, columns, constraints, indexes,
triggers, functions, enums, and comments a database holds, without dumping ~14MB
and reaching for jq. They read the exact same cached document (parsing it costs
~0.2s), so they cost a connection only when the cache is cold or stale.

Adds NO disclosure surface: anyone who can run a query here can already read
`pg_catalog` directly. This is a convenience wrapper over statements the caller
is already authorized to run.
"""

import argparse
import json
import sys

from .flags import add_env_flags, selected_env
from .. import app
from ..console import fail
from ..core import query, schema, store, tokens
from ..core.store import discover_envs
from ..core.system import in_system_mode

# The browse subcommands, dispatched on argv[0]. Everything else (including no
# argument at all) is the JSON dump, so `schema --dev` keeps its old meaning and
# nothing that scripts against it breaks.
BROWSE = {"list", "ls", "show", "find"}

# A single `find` category can match thousands of columns (search "id"); cap each
# so the terminal stays readable, and SAY when the cap bit rather than trailing
# off silently as if that were everything.
FIND_CAP = 60


def _add_source_flags(parser: argparse.ArgumentParser, envs: list):
    """The flags every schema path shares: which database, and cache freshness.

    Same env/token selection as the exec path, plus the two cache knobs — so
    `schema show public.users --dev --refresh` re-reads after a migration just
    like the dump does.
    """
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN",
                       help="use an ephemeral access token instead of an "
                            f"environment (see `{app.current().name} token --help`)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-introspect now, ignoring any cached copy")
    parser.add_argument("--max-age", metavar="AGE", default=None,
                        help="serve a cached copy only if younger than AGE "
                             "(45s/30m/2h/1d; default "
                             f"{schema.DEFAULT_MAX_AGE_SECONDS // 60}m, 0 to bypass)")


def build_parser(envs: list) -> argparse.ArgumentParser:
    # The DUMP parser. Deliberately no -o/--format and no pager, unlike the exec
    # path. Every renderer there is row-shaped and none of them fits a nested
    # document, and the consumer wants JSON — a format flag would only offer worse
    # answers to a question nobody has. A pager would be worse still: this is
    # megabytes of machine input, so paging it means an interactive prompt in
    # front of a tool.
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
            "To browse it by eye instead, see the list/show/find subcommands\n"
            f"(`{name} schema list --help`). The result is cached, so repeated\n"
            "calls do not re-introspect."
        ),
        epilog="examples:\n"
               f"  {name} schema --dev > schema.json\n"
               f"  {name} schema --dev | python -m json.tool\n"
               f"  {name} schema list --dev              # browse: schemas + counts\n"
               f"  {name} schema show public.users --dev # browse: one table in full\n"
               f"  {name} schema find email --dev        # browse: search by name\n"
               f"  {name} schema --dev --refresh         # re-read after a migration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_source_flags(parser, envs)
    parser.add_argument("--meta", action="store_true",
                        help="report cache status (cached/refreshed) on stderr")
    return parser


def build_browse_parser(action: str, envs: list) -> argparse.ArgumentParser:
    """One parser per browse subcommand, differing only in the positional it takes."""
    name = app.current().name
    parser = argparse.ArgumentParser(
        prog=f"{name} schema {action}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if action in ("list", "ls"):
        parser.description = (
            "List the schemas in a database, or the objects in one schema.\n\n"
            "With no name: every schema, with how many tables, views, and\n"
            "functions each holds. With a schema name: the tables, views,\n"
            "functions, and enums inside it."
        )
        parser.epilog = ("examples:\n"
                         f"  {name} schema list --dev            # all schemas + counts\n"
                         f"  {name} schema list public --dev     # objects in 'public'")
        parser.add_argument("schema", nargs="?", metavar="SCHEMA",
                            help="a schema name to list the contents of "
                                 "(omit to list all schemas)")
    elif action == "show":
        parser.description = (
            "Show one object in full. For a table or view: its columns (type,\n"
            "nullability, default, comment), constraints, indexes, triggers, and\n"
            "comment. For an enum: its values. For a function: its signature(s).\n\n"
            "Name it as schema.name; a bare name is resolved across all schemas."
        )
        parser.epilog = ("examples:\n"
                         f"  {name} schema show public.users --dev\n"
                         f"  {name} schema show users --dev       # search every schema\n"
                         f"  {name} schema show order_status --dev  # an enum's values")
        parser.add_argument("target", metavar="NAME",
                            help="the object to show, as schema.name or a bare name")
    else:  # find
        parser.description = (
            "Search names across the whole database — schemas, tables, columns,\n"
            "functions, enums, and enum values — for a case-insensitive substring."
        )
        parser.epilog = ("examples:\n"
                         f"  {name} schema find tenant --dev\n"
                         f"  {name} schema find _at --dev         # timestamp columns")
        parser.add_argument("term", metavar="TERM",
                            help="a substring to look for (case-insensitive)")
    _add_source_flags(parser, envs)
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


def _resolve_url(args, envs: list) -> str:
    """The database URL for this invocation, from an env (maybe prompting) or a token.

    Kept OUT of the introspection try below and BEFORE it: a store or token
    failure is not an introspection failure, and resolving an encrypted env
    prompts for a password — which a malformed `--max-age` should have already
    rejected. Callers parse_max_age first, for that reason.
    """
    if args.token:
        return tokens.load_database_url_from_token(args.token)
    return store.load_database_url(selected_env(args, envs))


def _load(args, envs: list) -> schema.SchemaResult:
    """Fetch the document (from cache when fresh), with the shared error split.

    Only `schema.load` is inside the try: a database error is the only thing
    whose disclosure the caller must decide. Over sudo the caller may be an
    agent, and a psycopg2 CONNECTION error can echo host/user/dbname — so that
    stays withheld. A SERVER-side error (one with a SQLSTATE) only describes the
    caller's own statement, and withholding that would leave them with "it
    failed" and nowhere to go. This mirrors commands/exec.py exactly.
    """
    max_age = parse_max_age(args.max_age)
    database_url = _resolve_url(args, envs)
    try:
        return schema.load(database_url, max_age=max_age, refresh=args.refresh)
    except Exception as e:
        if in_system_mode():
            detail = query.server_error(e)
            fail(f"Schema introspection failed: {detail}" if detail
                 else "Schema introspection failed")
        print(f"Schema introspection failed: {e}", file=sys.stderr)
        sys.exit(1)


# --- rendering: pure functions over the parsed document, so they unit-test
#     against a small dict fixture with no database in sight. ---

_KIND_LABEL = {
    "table": "table", "partitioned_table": "table",
    "view": "view", "materialized_view": "matview", "foreign_table": "table",
}


def _aligned(rows: list) -> list:
    """Left-justify a list of same-length string tuples into aligned columns."""
    if not rows:
        return []
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
            for row in rows]


def _column_flags(col: dict) -> str:
    """The terse right-hand annotations on a column line: not null, identity, ..."""
    parts = []
    if col.get("not_null"):
        parts.append("not null")
    if col.get("identity"):
        parts.append("identity")
    if col.get("generated"):
        parts.append("generated")
    if col.get("default") is not None:
        parts.append(f"default {col['default']}")
    return "  ".join(parts)


def render_schema_list(doc: dict) -> str:
    """Every schema, with a count of the tables, views, and functions it holds."""
    counts = {name: {"table": 0, "view": 0, "func": 0} for name in doc["schemas"]}
    for t in doc["tables"]:
        bucket = counts.setdefault(t["schema"], {"table": 0, "view": 0, "func": 0})
        bucket["view" if t["kind"] in ("view", "materialized_view") else "table"] += 1
    for f in doc["functions"]:
        counts.setdefault(f["schema"], {"table": 0, "view": 0, "func": 0})["func"] += 1

    rows = [("SCHEMA", "TABLES", "VIEWS", "FUNCTIONS")]
    for name in sorted(counts):
        c = counts[name]
        rows.append((name, str(c["table"]), str(c["view"]), str(c["func"])))
    header = f"{len(doc['schemas'])} schemas in {doc['database']}"
    return header + "\n" + "\n".join(_aligned(rows))


def render_schema_contents(doc: dict, schema_name: str) -> str:
    """The objects inside one schema, grouped by kind."""
    if schema_name not in doc["schemas"]:
        near = [s for s in doc["schemas"] if schema_name.lower() in s.lower()]
        hint = f" Did you mean: {', '.join(sorted(near))}?" if near else ""
        fail(f"No schema named '{schema_name}' in {doc['database']}.{hint}")

    rels = [t for t in doc["tables"] if t["schema"] == schema_name]
    tables = sorted(t["name"] for t in rels if t["kind"] not in ("view", "materialized_view"))
    views = sorted(t["name"] for t in rels if t["kind"] in ("view", "materialized_view"))
    # Compact here on purpose: a full argument list per function runs off the
    # screen (see the customer schema), and the point of `list` is to scan. The
    # real signature and body are one `show` away. Sorted by identity arguments
    # so overloads keep a stable order even though the display collapses them.
    fns = sorted((f for f in doc["functions"] if f["schema"] == schema_name),
                 key=lambda f: (f["name"], f["identity_arguments"]))
    funcs = [_func_summary(f) for f in fns]
    enums = sorted(f"{e['name']} {{{', '.join(e['values'])}}}"
                   for e in doc["enums"] if e["schema"] == schema_name)

    out = [f"schema {schema_name} in {doc['database']}"]
    for label, items in (("tables", tables), ("views", views),
                         ("functions", funcs), ("enums", enums)):
        if items:
            out.append(f"\n{label} ({len(items)}):")
            out += [f"  {i}" for i in items]
    if not (tables or views or funcs or enums):
        out.append("  (empty)")
    return "\n".join(out)


def _render_relation(t: dict) -> str:
    kind = _KIND_LABEL.get(t["kind"], t["kind"])
    out = [f"{kind} {t['schema']}.{t['name']}"]
    if t.get("comment"):
        out.append(f"  -- {t['comment']}")

    col_rows = []
    for c in t["columns"]:
        line = (c["name"], c["type"], _column_flags(c))
        col_rows.append(line)
    if col_rows:
        out.append(f"\ncolumns ({len(col_rows)}):")
        aligned = _aligned([("NAME", "TYPE", "")] + col_rows)
        # Re-attach each column's comment (rare) after its aligned line.
        out.append(aligned[0])
        for c, line in zip(t["columns"], aligned[1:]):
            out.append(line + (f"   -- {c['comment']}" if c.get("comment") else ""))

    if t["constraints"]:
        out.append(f"\nconstraints ({len(t['constraints'])}):")
        out += _aligned([(c["name"], c["type"].replace("_", " "), c["definition"])
                         for c in t["constraints"]])
    if t["indexes"]:
        out.append(f"\nindexes ({len(t['indexes'])}):")
        out += _aligned([(i["name"], i["definition"]) for i in t["indexes"]])
    if t["triggers"]:
        out.append(f"\ntriggers ({len(t['triggers'])}):")
        out += _aligned([(g["name"], g["definition"]) for g in t["triggers"]])
    if t.get("view_definition"):
        out.append("\ndefinition:")
        out.append(t["view_definition"])
    return "\n".join(out)


def _render_enum(e: dict) -> str:
    out = [f"enum {e['schema']}.{e['name']}"]
    if e.get("comment"):
        out.append(f"  -- {e['comment']}")
    out.append(f"\nvalues ({len(e['values'])}):")
    out += [f"  {v}" for v in e["values"]]
    return "\n".join(out)


def _func_summary(f: dict) -> str:
    """One compact line for `list`: `name(3 args)`, or `name()` for none.

    The count stands in for the arguments the full signature would spell out;
    `show` prints those, and the body. A missing arg_count (a pre-v2 cache a
    consumer somehow kept) degrades to a bare `name(...)` rather than claiming
    zero.
    """
    n = f.get("arg_count")
    if n is None:
        return f"{f['name']}(...)"
    if not n:
        return f"{f['name']}()"
    return f"{f['name']}({n} arg{'' if n == 1 else 's'})"


def _render_functions(fns: list) -> str:
    f0 = fns[0]
    out = [f"function {f0['schema']}.{f0['name']}"
           + (f"  ({len(fns)} overloads)" if len(fns) > 1 else "")]
    for f in fns:
        out.append(f"\n  {f['name']}({f['arguments']})")
        out.append(f"    returns {f['returns']}  [{f['kind']}, {f['language']}]")
        if f.get("comment"):
            out.append(f"    -- {f['comment']}")
        # The full CREATE statement, printed as Postgres formats it. Null only
        # for aggregate/window functions, which have no body to give (see the
        # guard in core.schema's introspection query).
        if f.get("definition"):
            out.append("  definition:")
            out += [f"    {line}" for line in f["definition"].splitlines()]
    return "\n".join(out)


def render_show(doc: dict, target: str) -> str:
    """Full detail for one object, resolved from a schema.name or a bare name.

    A relation wins over an enum or function of the same name; a bare name that
    matches in several schemas lists the candidates rather than guessing.
    """
    schema_name, _, name = target.rpartition(".")

    def matches(items):
        if schema_name:
            return [x for x in items if x["schema"] == schema_name and x["name"] == name]
        return [x for x in items if x["name"] == name]

    rels = matches(doc["tables"])
    if len(rels) == 1:
        return _render_relation(rels[0])
    if len(rels) > 1:
        opts = ", ".join(sorted(f"{t['schema']}.{t['name']}" for t in rels))
        fail(f"'{name}' is in several schemas: {opts}. Qualify it as schema.name.")

    enums = matches(doc["enums"])
    if enums:
        return _render_enum(enums[0])

    fns = matches(doc["functions"])
    if fns:
        return _render_functions(sorted(fns, key=lambda f: f["identity_arguments"]))

    where = f"{schema_name}." if schema_name else "any schema of "
    fail(f"No table, view, enum, or function named '{name}' in {where}{doc['database']}. "
         f"Try `{app.current().name} schema find {name}`.")


def _capped(items: list) -> list:
    """Trim a category to FIND_CAP, appending an honest note when it bit."""
    if len(items) <= FIND_CAP:
        return items
    return items[:FIND_CAP] + [f"... and {len(items) - FIND_CAP} more"]


def render_find(doc: dict, term: str) -> str:
    """Every name matching `term`, grouped by object kind. Case-insensitive."""
    q = term.lower()
    groups = []

    schemas = sorted(s for s in doc["schemas"] if q in s.lower())
    tables = sorted(f"{t['schema']}.{t['name']}" for t in doc["tables"]
                    if q in t["name"].lower())
    columns = sorted(f"{t['schema']}.{t['name']}.{c['name']}  ({c['type']})"
                     for t in doc["tables"] for c in t["columns"]
                     if q in c["name"].lower())
    funcs = sorted(f"{f['schema']}.{f['name']}({f['identity_arguments']})"
                   for f in doc["functions"] if q in f["name"].lower())
    enums = sorted(f"{e['schema']}.{e['name']}" for e in doc["enums"]
                   if q in e["name"].lower())
    enum_values = sorted(f"{e['schema']}.{e['name']} -> {v}" for e in doc["enums"]
                         for v in e["values"] if q in v.lower())

    for label, items in (("schemas", schemas), ("tables/views", tables),
                         ("columns", columns), ("functions", funcs),
                         ("enums", enums), ("enum values", enum_values)):
        if items:
            groups.append(f"{label} ({len(items)}):")
            groups += [f"  {i}" for i in _capped(items)]
            groups.append("")

    if not groups:
        return f"No schema, table, column, function, or enum matches '{term}'."
    return "\n".join(groups).rstrip()


def _run_dump(argv: list, envs: list):
    args = build_parser(envs).parse_args(argv)
    result = _load(args, envs)

    # A byte copy through the binary buffer, never print(): the document is
    # megabytes of exactly what Postgres produced, and going through the text
    # layer to re-encode it would cost time and give nothing back. The trailing
    # newline is ours — jsonb::text emits none.
    sys.stdout.buffer.write(result.document)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()

    if result.cache_written is False:
        print("Warning: the schema could not be cached; the next call will "
              "introspect again.", file=sys.stderr)
    if args.meta:
        print(f"cached (age {_age_text(result.age)})" if result.cached
              else f"refreshed in {result.elapsed:.1f}s", file=sys.stderr)


def _run_browse(action: str, argv: list, envs: list):
    args = build_browse_parser(action, envs).parse_args(argv)
    doc = json.loads(_load(args, envs).document)

    if action in ("list", "ls"):
        text = (render_schema_contents(doc, args.schema) if args.schema
                else render_schema_list(doc))
    elif action == "show":
        text = render_show(doc, args.target)
    else:  # find
        text = render_find(doc, args.term)
    print(text)


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             f"`{app.current().name} config set <name>`.")
    # Dispatch on the first token, exactly as cli.py does for the top level. A
    # browse verb routes to the browsers; anything else — including no argument —
    # is the JSON dump, so `schema --dev` keeps meaning what it always did.
    if argv and argv[0] in BROWSE:
        _run_browse(argv[0], argv[1:], envs)
    else:
        _run_dump(argv, envs)
