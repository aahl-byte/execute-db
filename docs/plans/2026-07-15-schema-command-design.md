# `schema` Command Design — Cached Introspection for External Tools

**Status:** approved design, not yet implemented. Hand to `writing-plans` for the task breakdown.

**Goal:** Add a `schema` subcommand to the shared engine so an external tool can pull a complete, machine-readable description of a database — tables, views, columns, constraints, indexes, enums, domains, functions, sequences, triggers, comments — and use it for auto-complete, linting, option hints, and UI search. Results are cached with a TTL so repeated calls do not re-introspect.

**Architecture:** A new `db_core/core/schema.py` (introspection + cache, pure logic) under a new `db_core/commands/schema.py` (argparse + output), dispatched from `cli.py` — mirroring the existing `core`/`commands` split. Both front-ends get the command from the shared engine; `explore-db` is the expected primary consumer. No new dependencies.

---

## Measurements (dev database, 2026-07-15)

Taken against the real `dev` environment via `explore-db --dev`. These numbers drove the design and are worth re-checking if it ever feels slow.

| Metric | Value |
| --- | --- |
| Relations | 2,127 (1,567 tables, 557 views, 3 matviews) |
| Partition children | 0 |
| Schemas | 35 |
| Columns | 31,612 |
| Indexes / constraints / triggers | 2,178 / 2,004 / 201 |
| Functions / enums | 535 / 14 |
| View definition text | 4.85 MB |
| Introspection time | ~2-4s |

Payload size by variant:

| Variant | Bytes |
| --- | --- |
| `jsonb_pretty`, nulls kept | 18.1 MB |
| compact, nulls kept | **11.1 MB (chosen)** |
| compact + `jsonb_strip_nulls` | 8.9 MB |
| compact, stripped, no view definitions | 3.8 MB |
| light index (names/types/keys only) | 2.5 MB |

> **Reconciliation (added after implementation).** These variants were measured with a probe query that built only `tables` and `enums`, so they omit domains, functions, sequences, and extensions. The **shipped** command produces **11.7 MB** on the same database — that is the number the README quotes, and the honest one. The table remains useful for the *relative* cost of each decision (pretty-printing is ~39%, view definitions ~5 MB), which is what it was built to settle.
>
> Likewise "35 schemas" here counts namespaces that *contain relations*; the document's `schemas` key counts all non-system namespaces, hence 36. Both are correct; they answer different questions.

---

## Locked design decisions

1. **Hand-written catalog queries, not `pg_dump` or SQLAlchemy.** Postgres has no server-side function that reconstructs a `CREATE TABLE`; `pg_dump` builds that text client-side from the same catalogs we would query, then throws the structure away. Auto-complete needs structure, so parsing DDL text back is strictly worse than querying for it. SQLAlchemy reflection is a heavy dependency that still misses enum values, comments, and function signatures.

2. **One statement, one JSON document.** All introspection runs as a single SQL statement, so the result is a consistent snapshot rather than a stitched-together set of moments that disagree.

3. **The full document is served, always.** No tiering, no `--table` projection, no `--schema` scoping. The consuming tool loads the document once per refresh and re-indexes it into its own query structure, so this is a bulk load, not a hot path. The light-index variants above were measured and deliberately rejected as unnecessary complexity.

4. **Compact JSON, nulls kept.** `jsonb_pretty` was 7 MB of whitespace for a machine consumer — dropped, at no contract cost. `jsonb_strip_nulls` would save a further 2.2 MB but makes keys vanish rather than be present-and-null; the rigid, fully-populated shape is worth 2.2 MB because it is easier to write a strict typed loader against.

5. **The cache stores raw JSON bytes, exactly as Postgres returned them.** A cache hit is a byte copy to stdout — no `json.loads`, no re-serialize — so a hit costs ~30ms regardless of document size. This is what makes an 11 MB payload a non-issue.

6. **File mtime is the fetch time.** No `fetched_at` field, no metadata sidecar. Staleness is `time.time() - path.stat().st_mtime > ttl`.

7. **`schema_version` lives in the filename** (`<urlhash>.v1.json`), so bumping the version misses the old cache instead of serving a stale shape to a tool that cannot tell.

8. **The cache is keyed by `sha256(database_url)[:12]`, not env name.** An env and a token pointing at the same database share one entry. The URL itself is never written to disk — only its digest.

9. **Introspection always opens a read-only transaction, even under `execute-db`.** `core/query.py` currently takes this from the `AppSpec`; introspection has no reason to ever write, so it should be structurally incapable of it rather than trusting a flag.

10. **Partition children would be excluded if any existed.** The dev database has none, so this is not implemented — noted only because the first payload estimate assumed a partition explosion and was wrong. If a future database has partitions, auto-complete wants `events`, not `events_2024_03`.

11. **Extension-owned functions are excluded** via `pg_depend deptype='e'`. Installing PostGIS would otherwise add thousands of functions nobody is completing against.

---

## Command surface

```
execute-db schema --dev              # or explore-db schema --dev
explore-db schema --token <TOKEN>
explore-db schema --dev --refresh    # force re-introspection
explore-db schema --dev --max-age 30m
explore-db schema --dev --max-age 0  # bypass cache
explore-db schema --dev --meta       # cache status on stderr
```

- Default TTL **15m**. `--max-age` reuses the existing `45s/30m/2h/1d` parser from `core/tokens.py`.
- **Output is the JSON document on stdout, always.** No `-o` formats — the existing renderers are all row-shaped and none fit a nested document. Never paged.
- `--meta` puts `cached (age 3m)` / `refreshed in 2.4s` on **stderr**, matching the project's existing rule: stdout carries only data, so it pipes straight into a JSON parser.

## Data flow

1. Resolve the database URL exactly as the exec path does (env file, prompting for a password if encrypted; or `--token`).
2. Key = `sha256(url)[:12]`. Cache path = `<config_dir>/cache/<key>.v1.json`, mode 600 in a 700 directory.
3. Fresh cache hit → copy bytes to stdout, no connection opened.
4. Otherwise introspect in one read-only transaction, write the cache via tmp-plus-`replace` (as `store.write_encrypted` already does, so a crash cannot leave a torn document), and emit.

The cache is plaintext because a schema is not a credential. In hardened mode it lands in the service user's home and is unreadable to the calling user anyway — stdout is the interface, not the file.

## Error handling

- Query failures reuse `query.server_error()` and follow `commands/exec.py:243` exactly: server-side errors disclosed, connection-level errors withheld in hardened mode. No new disclosure policy.
- **`schema` adds no new disclosure surface.** Anyone who can run `execute-db --dev "SELECT ..."` can already read `information_schema` and `pg_catalog` directly; this is a convenience wrapper over queries the caller is already authorized to run.
- A corrupt or unreadable cache file is treated as a miss and re-introspected. If the cache *write* fails, the document still goes to stdout with a warning on stderr — caching is an optimization, serving the schema is the job.
- **Not in v1:** serving a stale cache when the database is unreachable. Useful for a UI, easy to add, but it means printing data while hiding a failure and deserves its own decision.

## Security constraint on future flags

The sudoers rule is `<user> ALL=(<svc>) NOPASSWD: <cli> *` (`install.sh:194`). That trailing wildcard means **every flag is directly reachable as the service user**, bypassing the trusted launcher. This is why `-f/--file` is refused when `in_system_mode()` (`commands/exec.py:222`): the launcher normally strips `-f` and pipes the file in as the calling user, so an `-f` that reaches the service process would be an arbitrary-file-read primitive against the very store the hardening protects — and the server's syntax error would echo the file's contents back.

`schema` takes no file input and derives its cache path internally from a URL hash rather than from user input, so it stays out of this category. **Any future flag that names a path needs an `in_system_mode()` guard.**

## Other touched code

- `cli.py`: dispatch `schema`, add a section to `TOP_LEVEL_HELP`.
- `core/store.py:53`: add `"schema"` to `RESERVED_NAMES`, or an env named `schema` would shadow the subcommand. **Migration note:** an existing env named `schema` would start being ignored with a warning.
- `commands/config.py`: `config rm` wipes the whole cache directory. Entries are keyed by URL hash, so it cannot identify "its own" entry without decrypting the env first; the cache is cheap to regenerate, so clearing all of it is simpler and costs one refresh.

## Testing

The existing suite runs without a database and this keeps that true for everything except the SQL itself.

- **Unit (no DB):** cache keying, TTL staleness, hit/miss/`--refresh` decisions, corrupt-cache recovery, atomic write, cache-write-failure still emits, `--max-age` parsing, version-in-filename miss.
- **Integration (real DB):** the catalog SQL, skipped unless `EXECUTE_DB_TEST_URL` is set — honest about coverage, and CI stays green.
