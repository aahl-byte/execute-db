---
title: Schema Browse
summary: The `schema list`/`show`/`find` subcommands read the same cached document as human-facing text, so you can see what a database holds without dumping ~14 MB and reaching for jq.
intent: |
  The cached schema document was built for a program to load once and re-index
  (see SCHEMA_INTROSPECTION.md). But the person at the terminal wants the same
  facts — what schemas, tables, columns, constraints, triggers, functions, and
  comments are here — and dumping ~14 MB of JSON at them is a poor way to answer.
  These subcommands are the human face of that cache: they parse the document the
  dump serves and render it as psql-like text, drilling from an overview down to
  one object. They exist so that "what's in this database?" is a glance, not a
  pipeline.
parent: SCHEMA_INTROSPECTION.md
children: []
sources:
  - db_core/commands/schema.py
  - db_core/cli.py
  - tests/test_schema.py
tags: [schema, browse, cli, human]
---

# Schema Browse

`schema list`, `schema show`, and `schema find` turn the cached introspection
document (`SCHEMA_INTROSPECTION.md`) into something a person reads. They are the
human counterpart to the bare `schema` **dump**, which targets a program.

```
schema list --dev                 every schema, with table/view/function counts
schema list public --dev          the tables, views, functions, enums in one schema
schema show public.users --dev    one object in full
schema find email --dev           substring search across every name
```

## Dispatch: verbs, and everything else is the dump

`commands/schema.py:run` switches on the first token. `list`/`ls`/`show`/`find`
route to the browsers; **anything else — including no argument at all — is the
JSON dump.** This is the load-bearing contract: `schema --dev` keeps meaning
"dump the whole document as JSON," so nothing scripting against it broke when the
verbs were added. The dispatch mirrors `cli.py`'s own top-level argv[0] routing.

Explicit verbs were chosen over a single do-what-I-mean positional (where
`schema public` would list a schema and `schema public.users` would show a
table). The verb form was kept deliberately: it leaves the bare-noun default free
to stay the machine dump, and it makes each action’s intent legible rather than
inferred from the shape of the argument.

## It reads the cache, never the database directly

A browse loads through `schema.load` exactly as the dump does, then
`json.loads` the result. So it costs a connection only when the cache is **cold
or stale**, and `--refresh` / `--max-age` behave identically to the dump. Two
consequences worth holding onto:

- **The raw-bytes property is the dump's alone.** The dump copies Postgres's
  bytes to stdout with no parse (`SCHEMA_INTROSPECTION.md` explains why that
  matters). A browse *must* `json.loads` the whole ~14 MB — about 0.2s — because
  it needs the structure. That parse is the price of rendering, and it is why the
  browsers are not on the hot path the raw-bytes design optimizes.
- **Rendering is pure functions over the parsed dict.** `render_schema_list`,
  `render_schema_contents`, `render_show`, `render_find`, and their helpers take
  a plain dict and return a string. They touch no database and no filesystem, so
  `tests/test_schema.py` exercises every rendering branch against a small fixture
  document with no server in sight. The house split holds: the core produces and
  caches the document; the command layer parses and renders it, which is
  presentation.

## Progressive disclosure, especially for functions

The guiding rule is *compact in the overview, complete on drill-in*. It shows up
most in functions, which is why the document carries the fields it does.

- `schema list <schema>` renders each function as `name(3 args)` (or `name()`
  for none) via `_func_summary`. The full argument list is deliberately withheld
  here: a 9-argument signature runs off the screen, and the point of `list` is to
  scan.
- `schema show <function>` prints the real signature(s), the return type,
  language, comment, **and the whole `CREATE` body**.

That drill-in is a cache-only read *because* the introspection document already
carries `arg_count` and `definition` per function (added in `SCHEMA_VERSION` 2 —
see the parent). `show` never opens a second connection to fetch a body. A
function with no body — an aggregate or window function, whose definition the
server refuses to synthesize — simply omits the `definition:` block rather than
erroring.

## `show`: how a name resolves

`render_show` accepts either `schema.name` or a bare `name`:

- A **relation** (table/view/matview) wins over an enum or function of the same
  name — it is what people mean by far most often.
- A **bare name matching several schemas** lists the candidates and refuses to
  guess (`'users' is in several schemas: ...  Qualify it as schema.name`).
- **Enums** show their values; **functions** show every overload's signature and
  body.
- **Nothing found** fails with a pointer to `schema find <name>`, because a
  substring search is the natural next move.

## `find`: search that will not silently truncate

`render_find` matches a case-insensitive substring across schema names,
table/view names, column names, function names, enum names, **and enum values**,
grouped by kind. Each category is capped at `FIND_CAP` (60) and, when the cap
bites, appends an honest `... and N more` rather than trailing off as if that
were everything — the project's no-silent-caps ethos. Searching `id` returns
thousands of columns; the cap keeps the terminal usable and says so.

## Flags, disclosure, and what browse does *not* accept

Browse shares the source and freshness flags with the dump — `--dev`/`--token`
to pick the database, `--refresh` and `--max-age` to control the cache — through
`_add_source_flags`. It deliberately does **not** take `--meta` or `-o/--format`;
those are dump concerns, and passing them is a clean argparse error, not a crash.

Everything else follows the parent:

- `parse_max_age` runs **before** the URL is resolved, so a malformed
  `--max-age` is rejected before an encrypted environment prompts for a password
  (`SCHEMA_INTROSPECTION.md`).
- On failure, browse applies — does not define — the project's disclosure rule
  (`ERROR_DISCLOSURE.md`): only `schema.load` sits inside the `try`, a server
  error carrying a SQLSTATE is disclosed even over sudo, a connection-level
  failure stays withheld in hardened mode. Browse **adds no disclosure surface**:
  anyone who can browse can already read `pg_catalog`.

## Gotchas

- **`list`/`ls`/`show`/`find` are reserved as the first token.** To see a schema
  whose name happens to be a verb, reach it through another verb —
  `schema list find` lists the contents of a schema named `find`; only the
  bare-overview spelling of such a name is shadowed.
- **Every browse re-parses the whole document** (~0.2s). There is no per-object
  or per-schema cache below the document; the convenience is not worth another
  cache tier, and 0.2s is imperceptible for interactive use.
