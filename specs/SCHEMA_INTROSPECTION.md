---
title: Schema Introspection
summary: The `schema` command dumps a database's complete catalog as one JSON document, cached on disk as the exact bytes Postgres returned.
intent: An external tool — an editor, a linter, a schema-browsing UI — needs the whole shape of a database to drive auto-complete, linting, option hints, and search. It should get that as one consistent snapshot it can load and re-index, without paying seconds of catalog querying on every refresh. This subsystem produces that document from a single read-only statement and caches it so that repeated calls are a byte copy rather than a round trip.
parent: ARCHITECTURE.md
children: []
sources:
  - db_core/core/schema.py
  - db_core/commands/schema.py
  - tests/test_schema.py
  - tests/test_schema_integration.py
tags: [schema, introspection, cache, postgres]
---

# Schema Introspection

`execute-db schema --dev` (or `explore-db schema --dev`, which is the better home
for it) prints a complete, machine-readable description of a database as one JSON
document: tables, partitioned tables, views, materialized views and foreign
tables, each with columns, constraints, indexes, triggers and view definitions,
plus enums, domains, functions, sequences, extensions and a `schema_version`.
Expect megabytes — the development database this was built against produces about
11.7 MB.

The layering is the house split: `db_core/core/schema.py` is pure logic that
returns bytes and facts; `db_core/commands/schema.py` owns argparse, stderr
chatter, and the disclosure decision; `db_core/cli.py` dispatches. Both
front-ends inherit the command from the shared engine.

## Who this is for, and what follows from it

The reader is a **program**, not a person. It loads the document once per refresh
and re-indexes it into whatever structure it actually queries. Nearly every
surface decision falls out of that one fact:

- **The whole document, always.** There is no `--table` or `--schema` projection.
  The consumer re-indexes regardless, so slicing here would only hand it a subset
  to work around. Light-index variants were measured during design and rejected
  as complexity buying nothing.
- **No `-o/--format`.** Every renderer on the exec path is row-shaped, and none
  of them fits a nested document. A format flag could only offer worse answers to
  a question nobody has.
- **Never paged.** This is machine input measured in megabytes; paging it means
  standing an interactive prompt in front of a tool.
- **stdout carries the JSON and nothing else.** Cache status, warnings and errors
  go to stderr, so `schema --dev | jq` and `schema --dev > schema.json` both
  compose. `commands/schema.py` writes through `sys.stdout.buffer`, never
  `print()` — an 11 MB `str` through the text layer costs a needless encode and
  copy for nothing gained. The trailing newline is ours; `jsonb::text` emits
  none, and a redirected file that does not end in one is unfriendly. Every JSON
  parser ignores it.

## One statement, one snapshot

`INTROSPECT_SQL` is a single statement. That is the point: it yields a consistent
snapshot rather than a set of moments that disagree with each other. It is
hand-written against `pg_catalog` rather than delegating to `pg_dump` or
SQLAlchemy reflection, because Postgres has no server-side function that
reconstructs a `CREATE TABLE` — `pg_dump` builds that text client-side from the
same catalogs and then throws the structure away, and auto-complete needs the
structure. Parsing DDL back out of a string is strictly worse than querying for
it. Constraints therefore carry both the `pg_get_constraintdef` text *and*
structured `columns`/`references` fields, so a consumer learns that this foreign
key points at that table's those columns without parsing anything.

Do not read the field list out of this spec; read it out of the SQL, which is the
only authority on it. What is worth writing down is why the SQL is shaped the way
it is.

### The `::text` cast is load-bearing

The statement ends `)::text AS schema`. psycopg2 parses a `jsonb` result column
into a Python **dict**; casting server-side hands back a **str** that
`introspect` encodes straight to bytes. Without the cast the entire raw-bytes
cache — the thing that makes an 11 MB document cheap — is silently defeated: the
document would be parsed on the way in and re-serialized on the way out. As a
bonus, `jsonb::text` is already compact, which is exactly what we want.

There is deliberately **no** `isinstance(document, str)` guard in `introspect`.
The only reachable cause of a non-`str` is someone dropping the cast, and a guard
would hand the resulting dict back as-is, breaking the `-> bytes` contract
somewhere far downstream. Failing loudly at the cause beats a dict escaping into
the cache; `tests/test_schema.py` pins both halves.

The fence on the cast asserts the SQL **ends with** it. An earlier version
checked `"::text" in INTROSPECT_SQL`, which matched `contype::text` further up
the query and let the one cast the design rests on be deleted with a green suite.
If you tighten or move this test, keep it anchored to the terminal cast.

### Every literal `%` must be doubled

The statement executes **with** a parameter (`schema_version`), and psycopg2
`%`-formats the whole query string whenever args are passed. So every literal `%`
in a `LIKE` pattern has to be written `%%` or `execute` raises at runtime. There
are three today, all `pg\_...%%` patterns. Add a `LIKE` and you inherit the rule.

This is fenced without a database by asserting that
`INTROSPECT_SQL % {"schema_version": 1}` does not raise — precisely the
formatting psycopg2 performs.

### What the query excludes, and why

- **Partition children** (`NOT c.relispartition`): auto-complete wants `events`,
  not `events_2024_03`.
- **Extension-owned functions** (`pg_depend`, `deptype='e'`): installing PostGIS
  would otherwise add thousands of entries nobody is completing against. The
  filter is qualified by `classid = 'pg_proc'::regclass`, and that qualification
  is load-bearing — OIDs are unique *per catalog*, not globally, so an
  unqualified `objid = p.oid` can match a row describing some other catalog's
  object that happens to share the number, silently dropping a real function from
  auto-complete.
- **System namespaces**, `pg_toast%`, `pg_temp%`.

Nulls are **kept**. `jsonb_strip_nulls` would save roughly 2 MB of 11.7, but it
makes keys *vanish* rather than be present-and-null. The rigid, fully-populated
shape is easier to write a strict typed loader against, and that is worth 2 MB to
a consumer that reads this once per refresh. Likewise no `jsonb_pretty`: it was
measured at about 39% of the payload, all of it whitespace for a machine.

### Byte-stable output

The `functions` aggregate orders by `(nspname, proname, identity_arguments)`, not
just the first two. `(nspname, proname)` is **not** unique — overloads tie, and a
tie orders arbitrarily, so an unchanged schema could produce different bytes on
every refresh. The tiebreak is drawn from the document's own visible content
rather than an internal OID, and `(proname, proargtypes, pronamespace)` being
uniquely indexed makes it total. If you add another aggregate, ask whether its
sort key is unique before trusting it.

## Introspection is always read-only

`introspect` connects with `options="-c default_transaction_read_only=on"`, even
under `execute-db`, whose `AppSpec` is read/write. Introspection has no reason to
ever write, so it is structurally incapable of writing rather than trusting the
flag that `core/query.py` reads. `sslmode="require"` matches the posture
`core.query` connects with.

The teardown is `rollback()` inside a `finally`, with `close()` inside a nested
`finally` beneath it. Each piece earns its place:

- **`rollback()`, not `commit()`** — this transaction is structurally incapable of
  change, so claiming there is work to keep would be a lie. (`core.query` commits
  only because it is one flow for reads *and* writes.)
- **In the `finally`, not on the success path** — relying on `close()`'s implicit
  rollback is a psycopg2 disposition detail that stops being true the day the
  connection comes from a pool, and the error path, where the transaction is
  aborted, is exactly where a pool would care most.
- **`close()` guarded beneath it** — a terminated backend makes `rollback()` raise
  too, and an unguarded throwing rollback ahead of `close()` would strand the
  connection entirely, the opposite of what the rollback is there to protect.

`introspect` also does not swallow exceptions, and neither does `load`: the
command layer decides how much of a database error may be disclosed, and it
cannot decide that about an exception the core has already caught.

## The cache

### It stores raw bytes

What lands on disk is the exact bytes Postgres returned. A hit is a byte copy to
stdout: no `json.loads`, no re-serialize, no re-encode. That is the whole reason
an 11.7 MB payload is a non-issue. Measured end to end through the real path
(launcher → service user → psycopg2 → cache → stdout): **cold 3.5s, warm 0.193s,
byte-identical by `cmp`.**

### Keyed by a hash of the URL

Entries live at `<config dir>/cache/<sha256(database_url)[:12]>.v<N>.json`, mode
`0600` in a `0700` directory. Two consequences, both intended:

- **An environment and a token pointing at the same database share one entry.**
  Verified live: a `--token` call reported `cached (age 36s)`, hitting the entry
  `--dev` had created.
- **The URL never touches the disk**, only its digest.

Note the failure mode here differs from the one tokens carry. A cache-key
collision fails **open** — two databases would share an entry — but at 48 bits and
a realistic handful of environments the probability is around 1e-11. This is not
a security boundary; it is a lookup key that happens to be a digest so the URL
stays off disk. Do not reason about it as if it were the token machinery (see
`EPHEMERAL_TOKENS.md`).

The cache is **plaintext** because a schema is not a credential. In the plain
install that does mean a process running as you can read your table and column
names, even for an environment whose `.env` is encrypted. Under the hardened
install (`PRIVILEGE_SEPARATION.md`) it lands in the service user's home and your
own account cannot read it either — there, as everywhere, **stdout is the
interface, not the file.**

### mtime is the fetch time

There is no metadata sidecar and no `fetched_at` field. The file's mtime *is*
when the document was fetched, so there is nothing to keep in sync with anything.
`cache_age` is `time.time() - st_mtime`, and a missing entry and an unreachable
one (EACCES, ELOOP) deliberately give the same answer — both mean "no usable
document here", and the caller's move is identical either way.

### `SCHEMA_VERSION` is in the filename

Bump `SCHEMA_VERSION` whenever the document's **shape** changes. Because it is
part of the filename, a bump **misses** the old entry rather than serving a stale
shape to a tool that cannot tell the difference. The version is also bound into
the query as a parameter rather than baked into the SQL text, so the document
always declares the version the code that produced it believes in.

### Caching is an optimization; serving is the job

Every failure degrades toward serving:

- A corrupt, truncated, empty or unreadable entry is a **miss**, never an error.
  It costs one re-introspection.
- `write_cache` returns a bool and **never raises**. If the write fails the
  document still goes to stdout, with a warning on stderr.
- Writes are tmp-plus-`replace` (the same idiom as `store.write_encrypted`), so a
  crash mid-write cannot leave a torn document that a later run would serve as
  truth. `write_cache` creates the parent of *the path it was handed*, not
  `cache_dir()` — reaching back to the latter would fail for any other path and
  leave a spurious `cache/` directory behind as a souvenir.
- Nothing is written until the document is in hand, so a failed refresh leaves
  the previous entry untouched.

### Clock handling is split by role

This is deliberate and easy to "simplify" into a bug:

- **`cache_age` uses `time.time()`** because it compares against an `st_mtime`,
  which is wall time by definition. Nothing else would agree with the filesystem.
- **`load`'s `elapsed` uses `time.monotonic()`** because it is a *duration*. Wall
  time can step under NTP mid-introspection and report a refresh that took two
  seconds as `refreshed in -1000.0s`.
- **`read_cache` clamps a negative age to a miss** (`not 0 <= age <= max_age`). A
  future mtime means a skewed clock, not a fresh document — and a negative age is
  younger than every `max_age`, so without the clamp the entry would be pinned as
  fresh, un-expirable, for the whole duration of the skew. That silently defeats
  `max_age`'s one contract. A spurious miss costs one re-introspection, the same
  degrade path a corrupt file takes.

`elapsed` times the **introspection only**, not the process end to end and not the
cache write. The caller waits on the database; the write is milliseconds against
seconds. Timing both would blame the disk for the query's cost, or the query for
the disk's. `--meta`'s `refreshed in 3.5s` is honest about the query — do not
reword it as end-to-end.

### `max_age=0` is not the bypass mechanism

Bypass is `load`'s control flow: `if not refresh and max_age > 0`. `refresh` and
`max_age=0` are the same idea to `load`, so they collapse into the one question it
asks, and neither ever reaches `read_cache`.

Handing `max_age=0` to `read_cache` would appear to work — but only by arithmetic
accident, since `0 <= age <= 0` requires an age of exactly `0.0`, which
`time.time()` never equals `st_mtime`. That would make bypass a property a reader
has to derive rather than read, and would overload `max_age` with a second job its
own docstring explicitly disclaims: it is a **freshness bound**, not a sentinel.
Deciding it in `load` also spares a pointless `stat()`.
`test_load_bypass_is_control_flow_not_arithmetic` asks the question that pins
this — *was the cache consulted at all?* — because the obvious test passes for the
wrong implementation.

Both bypasses still **write** the fresh document on the way out. `--refresh` means
"do not serve me a cached document", not "do not maintain the cache"; a bypass
that skipped the write would leave the stale bytes on disk for the next caller.

## `SchemaResult`'s contract

`cached` is the one field to switch on. The rest is detail for `--meta`, and two
of them bite:

- **`cache_written` is a tri-state.** `True` = written; `False` = **attempted and
  failed**; `None` = never attempted (a cache hit). The command warns on
  `is False` only. Spelling it `if not result.cache_written` fires the warning on
  every cache hit — the common path — which is the surest way to train someone to
  ignore a warning that only ever matters because it is rare.
- **`age` is best-effort even on a hit.** `load` re-stats the file after
  `read_cache` succeeds (one syscall is a better price than widening
  `read_cache`'s signature to carry a number its other callers do not want), and
  an entry cleared between the read and the stat yields `age=None`. So
  `age is None` does **not** mean "not cached" — only `cached` answers that — and
  `_age_text` needs its age-unknown arm rather than formatting a `None` into
  `age None` or crashing on `int(None)`.

`elapsed` and `cache_written` are meaningful in exactly one branch each and
default to `None` outside it, so `None` reads as "not that branch" rather than as
a value.

## Flags and ordering

`--max-age` accepts the `45s/30m/2h/1d` grammar plus a bare `0`. `parse_max_age`
lives in the command layer, not the core, because it is an argparse concern:
`load` takes a number of seconds, and `"30m"` is a spelling the terminal uses. It
builds on `tokens.parse_duration`, **not** `tokens.parse_ttl` — a cache lifetime
is not a credential lifetime, so neither `parse_ttl`'s refusal of zero nor its
hardened-mode 24h cap applies. `0` is special-cased because the grammar requires
a unit and zero has none that means anything different; `0s` falls out of the
grammar and works too.

`parse_max_age` runs **before** the URL is resolved. Resolving an encrypted
environment prompts for its password, and `schema --prod --max-age soon` asking
for the prod password only to then reject the flag is backwards. A malformed flag
is decided before anything reaches for a credential.

## Failure and disclosure

`schema` **adds no new disclosure surface**: anyone who can run
`execute-db --dev "SELECT ..."` can already read `pg_catalog` for themselves. This
is a convenience wrapper over statements the caller is already authorized to run.

On failure it **applies** the project's disclosure rule; it does not define it.
See `ERROR_DISCLOSURE.md` for the rule itself — in short, a server-side error
carries a SQLSTATE and only ever describes the caller's own statement, so it is
disclosed even over sudo, while a connection-level failure can echo
host/user/dbname and stays withheld in hardened mode. `query.server_error`
searches the whole `__context__` chain, which matters here because `introspect`
ends its transaction in a `finally`: a terminated backend raises twice, and the
`InterfaceError` from the tidying rollback is what propagates while the server's
own words survive only as context.

**Only `load()` sits inside the command's `try`.** Resolving the URL fails on its
own terms — a bad password is not an introspection failure — and sweeping it in
would relabel those messages as this one, or in hardened mode reduce them to the
bare string, stranding the caller with a lie about the database and no idea their
env file is unreadable. `test_a_store_failure_is_not_relabelled_as_an_introspection_failure`
is the boundary test; keep the `try` narrow.

A **failed stdout write** — `schema --dev | head`, or `> /dev/full` on a full
disk — is deliberately *outside* that `try` and is caught as `OSError` at
`cli.py:main`, one stderr line and exit 1. `BrokenPipeError` is an `OSError`, so
one handler covers both. The message is **generic** and does not name the write:
that same catch is broad enough to see a store `OSError`, and calling an
unreadable `.env.dev` "could not write to stdout" would be exactly the
mislabelling the boundary test above exists to prevent, relocated one layer up.
Widening the command's own `try` to cover the emit is the wrong fix — it would put
the emit inside a handler that reports database-disclosure errors, which a failed
write is not.

## Interactions

- **`config rm` clears the *whole* cache** (`clear_cache`), not one entry. Entries
  are URL-hash-keyed and carry no environment identity, so `rm` cannot pick out
  "its" entry without decrypting the environment first — and a cache this cheap to
  rebuild is not worth doing that for. See `CREDENTIAL_STORE.md`.
- **`"schema"` is a reserved environment name**, or an env named `schema` would
  shadow the subcommand.
- **`install.sh` skips `cache/` when migrating a store into hardened mode**: it is
  a directory, and the migrator refuses non-regular files. It is regenerable, so
  there is nothing to preserve. See `PRIVILEGE_SEPARATION.md`.
- **Any future flag that names a path needs an `in_system_mode()` guard.** The
  sudoers rule ends in a wildcard, so every flag is directly reachable as the
  service user, bypassing the trusted launcher — which is why the exec path
  refuses `-f` in hardened mode. `schema` stays out of that category because it
  takes no file input and derives its cache path internally from a URL hash rather
  than from user input.

## Known gaps

**The cache has no reaper.** `max_age` affects read freshness only; nothing ever
unlinks an entry except `config rm` (which clears everything) and a
`SCHEMA_VERSION` bump (which orphans rather than removes). Re-point an environment
at a different database and the old entry is immortal — roughly 11.7 MB filed
under a hash nothing will ever look up again. This is a **growth** problem, not a
staleness one: correctness is safe, because an entry nobody looks up is never
served. If it ever needs fixing, the fix is an age-based sweep, not a change to
`max_age`'s meaning.

**The gated integration test has never run.** `tests/test_schema_integration.py`
is the only thing that proves `INTROSPECT_SQL` parses against a real server and
returns the documented shape; everything else fakes psycopg2 and can only assert
against the SQL's *text* — and text assertions have already been fooled once (the
`::text` substring check above). It skips unless `EXECUTE_DB_TEST_URL` is set, and
under a hardened install that URL is unreadable from a user account **by design**,
so running it needs a plain install or a throwaway database. The SQL is not
entirely unproven — it runs end to end through the real command against the dev
database, which is where the 11.7 MB and 3.5s/0.193s numbers come from — but the
shape assertions themselves have never executed.

**Not implemented, deliberately:** serving a stale cache when the database is
unreachable. Useful for a UI, easy to add, but it means printing data while hiding
a failure, and that deserves its own decision rather than being smuggled in as a
cache tweak.
