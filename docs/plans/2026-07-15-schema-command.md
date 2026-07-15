# `schema` Command Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `schema` subcommand to the shared engine that emits a complete, machine-readable JSON description of a database (tables, views, columns, constraints, indexes, enums, domains, functions, sequences, triggers, comments), cached on disk with a TTL, for an external tool to consume for auto-complete / linting / option hints / UI search.

**Architecture:** New `db_core/core/schema.py` (one catalog query + the cache — pure logic) under a new `db_core/commands/schema.py` (argparse + output), dispatched from `cli.py`. This mirrors the existing `core`/`commands` split exactly. Both front-ends inherit the command from the shared engine; `explore-db` is the expected primary consumer. The cache stores the raw JSON bytes Postgres returned, so a cache hit is a byte copy to stdout with no parse.

**Tech Stack:** Python 3.9+, `argparse`, `psycopg2` (existing), `pytest` 8.x. No new dependencies.

**Design doc:** `docs/plans/2026-07-15-schema-command-design.md` — read it first. It records the measurements (2,127 relations, 31,612 columns, 11.1 MB payload) and the locked decisions.

---

## Locked decisions carried from the design

1. **Full document, always.** No tiering, no `--table`/`--schema` projection. The consuming tool re-indexes into its own structure, so this is a bulk load, not a hot path.
2. **Compact JSON, nulls kept.** No `jsonb_pretty` (7 MB of whitespace), no `jsonb_strip_nulls` (the rigid shape is worth 2.2 MB).
3. **Cache stores raw bytes**; a hit is a byte copy. File **mtime is the fetch time** — no metadata sidecar.
4. **`schema_version` in the filename** (`<urlhash>.v1.json`) so a version bump misses rather than serving a stale shape.
5. **Cache key is `sha256(database_url)[:12]`** — envs and tokens pointing at the same DB share an entry; the URL itself is never written to disk.
6. **Introspection always runs read-only**, even under `execute-db`.
7. **Cache failures never break the command.** Corrupt cache → miss. Failed write → still emit, warn on stderr.

## Critical implementation notes (read before Task 1)

- **The `::text` cast in the SQL is load-bearing.** psycopg2 automatically parses a `jsonb` result column into a Python `dict`. Casting to `text` server-side means we get a `str` back and can encode it straight to bytes — no parse, no re-serialize. Without the cast, decision 3 is silently defeated.
- **`jsonb::text` is already compact** (no pretty-printing), which is exactly what decision 2 wants.
- **`tokens.parse_ttl` cannot be reused as-is for `--max-age`:** it rejects `0` and applies the 24h system-mode cap, neither of which is right here. Task 1 extracts the shared parser.
- **Write to `sys.stdout.buffer`, not `print`.** An 11 MB `str` through `print` costs a needless encode+copy.

---

## Reference: current code shape

- `db_core/core/store.py` — `config_dir()` (honors `_dir_override` in tests), `RESERVED_NAMES` (line 53), `write_encrypted()` (the tmp-plus-`replace` idiom to copy), `load_database_url(env)`, `discover_envs()`.
- `db_core/core/tokens.py` — `TTL_RE`, `TTL_UNITS`, `parse_ttl()` (line 43), `load_database_url_from_token()`.
- `db_core/core/query.py` — `run_query()`, `server_error()` (the disclosure split).
- `db_core/commands/exec.py` — `run()` (line 204) is the template: resolve URL → do work → format. Error handling at line 236-247 is the disclosure precedent to copy.
- `db_core/commands/flags.py` — `add_env_flags(parser, envs)`, `selected_env(args, envs)`.
- `db_core/cli.py` — `TOP_LEVEL_HELP` (line 19), dispatch (line 112-117).
- `db_core/console.py` — `fail()`.
- `tests/conftest.py` — `store` fixture points `_dir_override` at a tmp dir and forces `in_system_mode()` False.
- `tests/test_explore.py` — `_FakeConn`/`_FakeCursor`/`captured_connect` is the psycopg2-faking idiom to copy.

---

## Task 1: Extract a duration parser from `parse_ttl`

`--max-age` needs `45s/30m/2h/1d` parsing but must accept `0` and must not apply the token TTL cap. Extract the shared part; keep `parse_ttl`'s messages byte-identical so existing tests keep passing.

**Files:**
- Modify: `db_core/core/tokens.py:43-53`
- Test: `tests/test_schema.py` (create)

**Step 1: Write the failing test**

```python
"""The `schema` command: introspection, caching, and the CLI surface."""

import pytest

from db_core.console import fail
from db_core.core import tokens


def test_parse_duration_units():
    assert tokens.parse_duration("45s") == 45
    assert tokens.parse_duration("30m") == 1800
    assert tokens.parse_duration("2h") == 7200
    assert tokens.parse_duration("1d") == 86400


def test_parse_duration_allows_zero_unlike_parse_ttl():
    # --max-age 0 means "bypass the cache"; --ttl 0 is still nonsense.
    assert tokens.parse_duration("0s") == 0
    with pytest.raises(SystemExit):
        tokens.parse_ttl("0s")


def test_parse_duration_names_the_flag_in_its_error(capsys):
    with pytest.raises(SystemExit):
        tokens.parse_duration("soon", flag="--max-age")
    assert "--max-age" in capsys.readouterr().err
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -v`
Expected: FAIL — `AttributeError: module 'db_core.core.tokens' has no attribute 'parse_duration'`

**Step 3: Write minimal implementation**

Replace `db_core/core/tokens.py:43-53` with:

```python
def parse_duration(text: str, flag: str = "--ttl") -> int:
    """Parse a `45s/30m/2h/1d` duration into seconds. Zero is allowed.

    Shared by `--ttl` (which additionally forbids zero and caps in system mode)
    and by `schema --max-age` (where zero legitimately means "bypass the cache").
    `flag` only names the option in the error message.
    """
    m = TTL_RE.match(text)
    if not m:
        fail(f"Invalid {flag} {text!r} (use e.g. 45s, 30m, 2h, 1d)")
    return int(m.group(1)) * TTL_UNITS[m.group(2)]


def parse_ttl(text: str) -> int:
    seconds = parse_duration(text, "--ttl")
    if seconds <= 0:
        fail(f"Invalid --ttl {text!r}: must be greater than zero")
    if system.in_system_mode() and seconds > system.MAX_SYSTEM_TTL_SECONDS:
        fail(f"--ttl {text!r} exceeds the {system.MAX_SYSTEM_TTL_SECONDS // 3600}h maximum "
             f"in hardened (system) mode")
    return seconds
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schema.py tests/test_config.py -v`
Expected: PASS — and the existing token tests must be unaffected.

**Step 5: Commit**

```bash
git add db_core/core/tokens.py tests/test_schema.py
git commit -m "refactor: extract parse_duration from parse_ttl"
```

---

## Task 2: Cache key and path

**Files:**
- Create: `db_core/core/schema.py`
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
from db_core.core import schema

URL_A = "postgresql://u:p@host/db_a"
URL_B = "postgresql://u:p@host/db_b"


def test_cache_key_is_stable_and_url_specific():
    assert schema.cache_key(URL_A) == schema.cache_key(URL_A)
    assert schema.cache_key(URL_A) != schema.cache_key(URL_B)
    assert len(schema.cache_key(URL_A)) == 12


def test_cache_path_never_contains_the_url(store):
    path = schema.cache_path(URL_A)
    assert "host" not in str(path) and "p@" not in str(path)
    assert path.parent == store / "cache"


def test_cache_path_carries_the_schema_version(store):
    assert schema.cache_path(URL_A).name.endswith(f".v{schema.SCHEMA_VERSION}.json")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k cache_key -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db_core.core.schema'`

**Step 3: Write minimal implementation**

Create `db_core/core/schema.py` with the module docstring, constants, and key/path helpers (the SQL constant lands in Task 5):

```python
"""Introspect a database's full schema, and cache the raw result.

The document is produced by ONE catalog query, so it is a consistent snapshot
rather than a set of moments that disagree. It is cached as the exact bytes
Postgres returned: a cache hit is a byte copy to stdout with no parse and no
re-serialize, which is what keeps an 11MB document cheap to serve.

Pure logic: this module returns bytes and facts. Formatting, flags, and stderr
chatter are the command layer's job.

The cache is keyed by a hash of the database URL rather than by environment
name, so an environment and a token pointing at the same database share one
entry — and the URL itself never touches the disk, only its digest. A schema is
not a credential, so the file is plaintext (mode 600); in hardened mode it lands
in the service user's home and is unreadable to the calling user anyway, which
is why stdout, not the file, is the interface.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2

from . import store

# Bump when the document's SHAPE changes. It is part of the cache filename, so a
# bump misses the old cache instead of serving a stale shape to a tool that
# cannot tell the difference.
SCHEMA_VERSION = 1

DEFAULT_MAX_AGE_SECONDS = 15 * 60  # default --max-age; a schema only moves on migration


def cache_key(database_url: str) -> str:
    return hashlib.sha256(database_url.encode()).hexdigest()[:12]


def cache_dir() -> Path:
    return store.config_dir() / "cache"


def cache_path(database_url: str) -> Path:
    return cache_dir() / f"{cache_key(database_url)}.v{SCHEMA_VERSION}.json"
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/core/schema.py tests/test_schema.py
git commit -m "feat: schema cache key and path"
```

---

## Task 3: Cache read and write

`read_cache` returns bytes or `None` (miss). `write_cache` returns whether it succeeded — the caller must be able to keep going when it didn't.

**Files:**
- Modify: `db_core/core/schema.py`
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
def test_write_then_read_round_trips(store):
    path = schema.cache_path(URL_A)
    assert schema.write_cache(path, b'{"tables": []}') is True
    assert schema.read_cache(path, max_age=60) == b'{"tables": []}'


def test_cache_file_is_owner_only_in_an_owner_only_dir(store):
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b"{}")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_write_leaves_no_tmp_file_behind(store):
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b"{}")
    assert list(path.parent.glob("*.tmp")) == []


def test_read_cache_misses_when_stale(store):
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b"{}")
    import os
    old = time.time() - 3600
    os.utime(path, (old, old))          # mtime IS the fetch time
    assert schema.read_cache(path, max_age=60) is None
    assert schema.read_cache(path, max_age=7200) == b"{}"


def test_read_cache_misses_when_absent_or_empty(store):
    path = schema.cache_path(URL_A)
    assert schema.read_cache(path, max_age=60) is None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(b"")               # truncated/corrupt -> treat as a miss
    assert schema.read_cache(path, max_age=60) is None


def test_write_cache_reports_failure_instead_of_raising(store, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(schema.Path, "mkdir", boom)
    assert schema.write_cache(schema.cache_path(URL_A), b"{}") is False
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k cache -v`
Expected: FAIL — `AttributeError: module 'db_core.core.schema' has no attribute 'write_cache'`

**Step 3: Write minimal implementation**

Append to `db_core/core/schema.py`:

```python
def cache_age(path: Path) -> "float | None":
    """Seconds since the document was fetched, or None if there is no file.

    The file's mtime IS the fetch time; there is deliberately no metadata
    sidecar and no `fetched_at` field to keep in sync with it.
    """
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def read_cache(path: Path, max_age: float) -> "bytes | None":
    """The cached document if it is younger than `max_age`, else None.

    Any unreadable or empty file is a miss, never an error: a truncated cache
    must degrade into a re-introspection, not a failure.
    """
    age = cache_age(path)
    if age is None or age > max_age:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def write_cache(path: Path, document: bytes) -> bool:
    """Cache `document`, atomically. Returns False if it could not be written.

    tmp-plus-replace (as `store.write_encrypted` does) so a crash mid-write
    cannot leave a torn document that a later run would serve as truth.
    Caching is an optimization, so every failure is reported, not raised.
    """
    try:
        cache_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(document)
        tmp.chmod(0o600)
        tmp.replace(path)
        return True
    except OSError:
        return False


def clear_cache() -> int:
    """Remove every cached document; returns how many went. Never raises.

    Entries are keyed by URL hash, so a caller removing one environment cannot
    identify "its" entry without decrypting the env first. The cache is cheap to
    regenerate, so clearing all of it is simpler and costs one refresh.
    """
    d = cache_dir()
    if not d.is_dir():
        return 0
    count = 0
    for p in d.glob("*.json"):
        try:
            p.unlink()
            count += 1
        except OSError:
            pass
    return count
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/core/schema.py tests/test_schema.py
git commit -m "feat: schema cache read/write/clear"
```

---

## Task 4: `clear_cache` test

**Files:**
- Test: `tests/test_schema.py`

**Step 1: Write the test** (implementation landed in Task 3 — it is one cohesive cache API)

```python
def test_clear_cache_removes_every_entry(store):
    schema.write_cache(schema.cache_path(URL_A), b"{}")
    schema.write_cache(schema.cache_path(URL_B), b"{}")
    assert schema.clear_cache() == 2
    assert schema.clear_cache() == 0        # idempotent, and no dir is fine
```

**Step 2: Run and commit**

Run: `python -m pytest tests/test_schema.py -k clear -v`
Expected: PASS

```bash
git add tests/test_schema.py
git commit -m "test: clear_cache removes every entry"
```

---

## Task 5: The introspection query

The big one. The SQL below is the query validated against the dev database in the design discussion, with two changes: `jsonb_pretty(...)` became `...::text` (compact, and `str` not `dict` — see the critical notes), and `schema_version` was added to the document.

**Files:**
- Modify: `db_core/core/schema.py`
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

Copy the `_FakeCursor`/`_FakeConn` idiom from `tests/test_explore.py:11-46`. The point of these tests is the *connection contract*, not the SQL (that is Task 11).

```python
from db_core import app
from db_core.core import query


class _FakeSchemaCursor:
    description = [("schema",)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return ('{"tables": []}',)     # psycopg2 hands back str for ::text


class _FakeSchemaConn:
    def __init__(self):
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return _FakeSchemaCursor()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def captured_schema_connect(monkeypatch):
    seen = {}

    def fake_connect(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        seen["conn"] = _FakeSchemaConn()
        return seen["conn"]

    monkeypatch.setattr(schema.psycopg2, "connect", fake_connect)
    return seen


def test_introspect_returns_raw_bytes(captured_schema_connect):
    assert schema.introspect(URL_A) == b'{"tables": []}'


def test_introspect_is_read_only_even_under_execute_db(captured_schema_connect):
    # The execute-db spec is read/write (conftest configures it), but
    # introspection must be structurally incapable of writing regardless.
    assert app.current().read_only is False
    schema.introspect(URL_A)
    assert "default_transaction_read_only=on" in captured_schema_connect["kwargs"]["options"]
    assert captured_schema_connect["kwargs"]["sslmode"] == "require"


def test_introspect_closes_the_connection(captured_schema_connect):
    schema.introspect(URL_A)
    assert captured_schema_connect["conn"].closed is True


def test_introspect_casts_to_text_so_psycopg2_does_not_parse_it(captured_schema_connect):
    # Without ::text psycopg2 parses jsonb into a dict and the raw-bytes cache
    # (and its no-parse cache hit) is silently defeated.
    schema.introspect(URL_A)
    assert "::text" in schema.INTROSPECT_SQL
    assert "jsonb_pretty" not in schema.INTROSPECT_SQL
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k introspect -v`
Expected: FAIL — `AttributeError: module 'db_core.core.schema' has no attribute 'introspect'`

**Step 3: Write minimal implementation**

Append to `db_core/core/schema.py`. Note `relispartition` is excluded: auto-complete wants `events`, not `events_2024_03`. The dev database has no partitions, so this is currently a no-op guard.

```python
# One statement -> one document -> one consistent snapshot.
#
# `::text` at the end is load-bearing: psycopg2 parses a jsonb result column
# into a Python dict, which would force a re-serialize and defeat the raw-bytes
# cache. Casting server-side gives us a str. jsonb::text is already compact.
#
# Nulls are deliberately KEPT (no jsonb_strip_nulls): the rigid, fully-populated
# shape is easier to write a strict typed loader against, and costs ~2MB.
INTROSPECT_SQL = r"""
WITH rels AS (
    SELECT c.oid, n.nspname, c.relname, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND NOT c.relispartition
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_toast%%'
      AND n.nspname NOT LIKE 'pg\_temp%%'
),
cols AS (
    SELECT r.oid, jsonb_agg(jsonb_build_object(
               'name', a.attname,
               'type', format_type(a.atttypid, a.atttypmod),
               'not_null', a.attnotnull,
               'default', pg_get_expr(d.adbin, d.adrelid),
               'identity', NULLIF(a.attidentity, ''),
               'generated', NULLIF(a.attgenerated, ''),
               'position', a.attnum,
               'comment', col_description(r.oid, a.attnum)
           ) ORDER BY a.attnum) AS columns
    FROM rels r
    JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid = r.oid AND d.adnum = a.attnum
    GROUP BY r.oid
),
cons AS (
    SELECT c.conrelid AS oid, jsonb_agg(jsonb_build_object(
               'name', c.conname,
               'type', CASE c.contype
                           WHEN 'p' THEN 'primary_key'
                           WHEN 'f' THEN 'foreign_key'
                           WHEN 'u' THEN 'unique'
                           WHEN 'c' THEN 'check'
                           WHEN 'x' THEN 'exclude'
                           ELSE c.contype::text END,
               'definition', pg_get_constraintdef(c.oid),
               'columns', (SELECT jsonb_agg(a.attname ORDER BY k.ord)
                           FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
                           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum),
               'references', CASE WHEN c.contype = 'f' THEN jsonb_build_object(
                   'table', (SELECT n.nspname || '.' || fc.relname
                             FROM pg_class fc JOIN pg_namespace n ON n.oid = fc.relnamespace
                             WHERE fc.oid = c.confrelid),
                   'columns', (SELECT jsonb_agg(a.attname ORDER BY k.ord)
                               FROM unnest(c.confkey) WITH ORDINALITY k(attnum, ord)
                               JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum)
               ) END
           ) ORDER BY c.conname) AS constraints
    FROM pg_constraint c
    JOIN rels r ON r.oid = c.conrelid
    GROUP BY c.conrelid
),
idx AS (
    SELECT i.indrelid AS oid, jsonb_agg(jsonb_build_object(
               'name', ic.relname,
               'definition', pg_get_indexdef(i.indexrelid),
               'unique', i.indisunique,
               'primary', i.indisprimary
           ) ORDER BY ic.relname) AS indexes
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN rels r ON r.oid = i.indrelid
    GROUP BY i.indrelid
),
trg AS (
    SELECT t.tgrelid AS oid, jsonb_agg(jsonb_build_object(
               'name', t.tgname,
               'definition', pg_get_triggerdef(t.oid)
           ) ORDER BY t.tgname) AS triggers
    FROM pg_trigger t
    JOIN rels r ON r.oid = t.tgrelid
    WHERE NOT t.tgisinternal
    GROUP BY t.tgrelid
),
tables AS (
    SELECT jsonb_agg(jsonb_build_object(
               'schema', r.nspname,
               'name', r.relname,
               'kind', CASE r.relkind
                           WHEN 'r' THEN 'table'
                           WHEN 'p' THEN 'partitioned_table'
                           WHEN 'v' THEN 'view'
                           WHEN 'm' THEN 'materialized_view'
                           WHEN 'f' THEN 'foreign_table' END,
               'comment', obj_description(r.oid, 'pg_class'),
               'columns', COALESCE(c.columns, '[]'::jsonb),
               'constraints', COALESCE(k.constraints, '[]'::jsonb),
               'indexes', COALESCE(x.indexes, '[]'::jsonb),
               'triggers', COALESCE(g.triggers, '[]'::jsonb),
               'view_definition', CASE WHEN r.relkind IN ('v', 'm')
                                       THEN pg_get_viewdef(r.oid, true) END
           ) ORDER BY r.nspname, r.relname) AS j
    FROM rels r
    LEFT JOIN cols c USING (oid)
    LEFT JOIN cons k USING (oid)
    LEFT JOIN idx x USING (oid)
    LEFT JOIN trg g USING (oid)
),
enums AS (
    SELECT jsonb_agg(jsonb_build_object(
               'schema', n.nspname,
               'name', t.typname,
               'values', (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder)
                          FROM pg_enum e WHERE e.enumtypid = t.oid),
               'comment', obj_description(t.oid, 'pg_type')
           ) ORDER BY n.nspname, t.typname) AS j
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typtype = 'e' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
),
domains AS (
    SELECT jsonb_agg(jsonb_build_object(
               'schema', n.nspname,
               'name', t.typname,
               'base_type', format_type(t.typbasetype, t.typtypmod),
               'not_null', t.typnotnull,
               'default', t.typdefault,
               'constraints', (SELECT jsonb_agg(pg_get_constraintdef(c.oid))
                               FROM pg_constraint c WHERE c.contypid = t.oid)
           ) ORDER BY n.nspname, t.typname) AS j
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typtype = 'd' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
),
funcs AS (
    SELECT jsonb_agg(jsonb_build_object(
               'schema', n.nspname,
               'name', p.proname,
               'kind', CASE p.prokind
                           WHEN 'f' THEN 'function'
                           WHEN 'p' THEN 'procedure'
                           WHEN 'a' THEN 'aggregate'
                           WHEN 'w' THEN 'window' END,
               'arguments', pg_get_function_arguments(p.oid),
               'identity_arguments', pg_get_function_identity_arguments(p.oid),
               'returns', pg_get_function_result(p.oid),
               'language', l.lanname,
               'comment', obj_description(p.oid, 'pg_proc')
           ) ORDER BY n.nspname, p.proname) AS j
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      -- extension-owned functions (PostGIS, pg_trgm, ...) are thousands of
      -- entries nobody is completing against.
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.objid = p.oid AND d.deptype = 'e')
),
seqs AS (
    SELECT jsonb_agg(jsonb_build_object(
               'schema', schemaname,
               'name', sequencename,
               'data_type', data_type,
               'start', start_value,
               'min', min_value,
               'max', max_value,
               'increment', increment_by,
               'cycle', cycle
           ) ORDER BY schemaname, sequencename) AS j
    FROM pg_sequences
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
),
exts AS (
    SELECT jsonb_agg(jsonb_build_object('name', extname, 'version', extversion)
           ORDER BY extname) AS j
    FROM pg_extension
)
SELECT jsonb_build_object(
    'schema_version', %(schema_version)s::int,
    'generated_at', now(),
    'database', current_database(),
    'server_version', current_setting('server_version'),
    'schemas', (SELECT jsonb_agg(nspname ORDER BY nspname) FROM pg_namespace
                WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                  AND nspname NOT LIKE 'pg\_%%'),
    'tables', COALESCE((SELECT j FROM tables), '[]'::jsonb),
    'enums', COALESCE((SELECT j FROM enums), '[]'::jsonb),
    'domains', COALESCE((SELECT j FROM domains), '[]'::jsonb),
    'functions', COALESCE((SELECT j FROM funcs), '[]'::jsonb),
    'sequences', COALESCE((SELECT j FROM seqs), '[]'::jsonb),
    'extensions', COALESCE((SELECT j FROM exts), '[]'::jsonb)
)::text AS schema
"""


def introspect(database_url: str) -> bytes:
    """Run the catalog query and return the document as raw JSON bytes.

    ALWAYS read-only, even under execute-db: introspection has no reason to
    write, so it should be structurally incapable of it rather than trusting the
    AppSpec flag that `core.query` reads.
    """
    conn = psycopg2.connect(
        database_url,
        sslmode="require",
        options="-c default_transaction_read_only=on",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(INTROSPECT_SQL, {"schema_version": SCHEMA_VERSION})
            (document,) = cur.fetchone()
        conn.rollback()  # nothing to commit; do not hold the snapshot open
        return document.encode() if isinstance(document, str) else document
    finally:
        conn.close()
```

**NOTE on `%%` — the trap in this task.** `INTROSPECT_SQL` is executed *with* a parameter (`schema_version`), and psycopg2 runs `%`-formatting over the whole query string whenever args are passed. So **every literal `%` in the SQL must be doubled**, or psycopg2 raises `ValueError: unsupported format character` / `IndexError` at execute time. There are exactly three, all `LIKE` patterns, and all three are already doubled in the constant above: `'pg\_toast%%'` and `'pg\_temp%%'` in `rels`, and `'pg\_%%'` in the `schemas` sub-select. If you add a `LIKE` pattern, double its `%` too.

This is fenced by a unit test that needs no database — `INTROSPECT_SQL % {"schema_version": 1}` must not raise, which is precisely the formatting psycopg2 performs:

```python
def test_introspect_sql_survives_psycopg2_percent_formatting():
    # psycopg2 %-formats the query when args are passed, so every literal % in
    # a LIKE pattern must be doubled. This is the cheap, DB-free fence for it.
    assert INTROSPECT_SQL % {"schema_version": 1}
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/core/schema.py tests/test_schema.py
git commit -m "feat: full-schema catalog introspection query"
```

---

## Task 6: `load()` — the cache/introspect orchestration

**Files:**
- Modify: `db_core/core/schema.py`
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
@pytest.fixture
def fake_introspect(monkeypatch):
    calls = []

    def _introspect(url):
        calls.append(url)
        return b'{"tables": ["fresh"]}'

    monkeypatch.setattr(schema, "introspect", _introspect)
    return calls


def test_load_miss_introspects_and_caches(store, fake_introspect):
    result = schema.load(URL_A)
    assert result.document == b'{"tables": ["fresh"]}'
    assert result.cached is False
    assert result.cache_written is True
    assert fake_introspect == [URL_A]
    assert schema.cache_path(URL_A).exists()


def test_load_hit_serves_cache_without_connecting(store, fake_introspect):
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    result = schema.load(URL_A)
    assert result.document == b'{"tables": ["cached"]}'
    assert result.cached is True
    assert result.age is not None
    assert fake_introspect == []          # never touched the database


def test_load_refresh_bypasses_a_fresh_cache(store, fake_introspect):
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    result = schema.load(URL_A, refresh=True)
    assert result.document == b'{"tables": ["fresh"]}'
    assert fake_introspect == [URL_A]


def test_load_max_age_zero_always_introspects(store, fake_introspect):
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    result = schema.load(URL_A, max_age=0)
    assert result.document == b'{"tables": ["fresh"]}'


def test_load_stale_cache_reintrospects(store, fake_introspect):
    import os
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b'{"tables": ["cached"]}')
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert schema.load(URL_A, max_age=60).document == b'{"tables": ["fresh"]}'


def test_load_still_serves_when_the_cache_cannot_be_written(store, fake_introspect,
                                                            monkeypatch):
    monkeypatch.setattr(schema, "write_cache", lambda p, d: False)
    result = schema.load(URL_A)
    assert result.document == b'{"tables": ["fresh"]}'    # serving is the job
    assert result.cache_written is False
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k load -v`
Expected: FAIL — `AttributeError: module 'db_core.core.schema' has no attribute 'load'`

**Step 3: Write minimal implementation**

Add the dataclass next to the other definitions at the top of `db_core/core/schema.py`, and `load()` at the end:

```python
@dataclass
class SchemaResult:
    document: bytes            # raw JSON, exactly as Postgres produced it
    cached: bool               # served from disk without connecting?
    age: "float | None" = None       # seconds since fetch, when cached
    elapsed: "float | None" = None   # seconds spent introspecting, when not
    cache_written: bool = False


def load(database_url: str, max_age: float = DEFAULT_MAX_AGE_SECONDS,
         refresh: bool = False) -> SchemaResult:
    """The document for `database_url`, from cache when fresh enough.

    `refresh` forces a re-read; `max_age=0` bypasses the cache entirely.
    """
    path = cache_path(database_url)
    if not refresh:
        document = read_cache(path, max_age)
        if document is not None:
            return SchemaResult(document=document, cached=True,
                                age=cache_age(path), cache_written=False)

    started = time.time()
    document = introspect(database_url)
    elapsed = time.time() - started
    return SchemaResult(document=document, cached=False, elapsed=elapsed,
                        cache_written=write_cache(path, document))
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/core/schema.py tests/test_schema.py
git commit -m "feat: schema load with TTL cache"
```

---

## Task 7: `"schema"` becomes a reserved environment name

Without this, an env named `schema` would shadow the subcommand.

**Files:**
- Modify: `db_core/core/store.py:53`
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
def test_schema_is_a_reserved_env_name(store):
    assert "schema" in store_mod.RESERVED_NAMES
    with pytest.raises(SystemExit):
        store_mod.validate_alias("schema")


def test_an_env_file_named_schema_is_ignored(store, capsys):
    (store / ".env.schema").write_text("DATABASE_URL=postgresql://x/y\n")
    (store / ".env.dev").write_text("DATABASE_URL=postgresql://x/y\n")
    assert store_mod.discover_envs() == ["dev"]
    assert "Ignoring invalid environment file" in capsys.readouterr().err
```

Add to the imports at the top of `tests/test_schema.py`:

```python
from db_core.core import store as store_mod
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k reserved -v`
Expected: FAIL — `assert 'schema' in {'password', 'token', ...}`

**Step 3: Write minimal implementation**

`db_core/core/store.py:53`:

```python
RESERVED_NAMES = {"password", "token", "config", "schema", "file", "f", "help", "sql"}
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/core/store.py tests/test_schema.py
git commit -m "feat: reserve 'schema' as an environment name"
```

---

## Task 8: The `schema` command

**Files:**
- Create: `db_core/commands/schema.py`
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
from db_core.commands import schema as schema_cmd


@pytest.fixture
def dev_env(store):
    (store / ".env.dev").write_text("DATABASE_URL=postgresql://u:p@host/dev\n")
    return store


def test_run_writes_the_document_to_stdout(dev_env, fake_introspect, capsysbinary):
    schema_cmd.run(["--dev"])
    out, err = capsysbinary.readouterr()
    assert out == b'{"tables": ["fresh"]}\n'
    assert err == b""                      # stdout carries data only


def test_meta_reports_cache_status_on_stderr(dev_env, fake_introspect, capsysbinary):
    schema_cmd.run(["--dev", "--meta"])
    assert b"refreshed in" in capsysbinary.readouterr()[1]
    schema_cmd.run(["--dev", "--meta"])
    assert b"cached" in capsysbinary.readouterr()[1]


def test_max_age_zero_is_accepted_and_bypasses_the_cache(dev_env, fake_introspect):
    schema_cmd.run(["--dev"])
    schema_cmd.run(["--dev", "--max-age", "0"])
    assert len(fake_introspect) == 2        # second call ignored a fresh cache


def test_max_age_rejects_nonsense(dev_env, fake_introspect, capsys):
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev", "--max-age", "soon"])
    assert "--max-age" in capsys.readouterr().err


def test_cache_write_failure_warns_but_still_emits(dev_env, fake_introspect,
                                                   monkeypatch, capsysbinary):
    monkeypatch.setattr(schema.schema_core if False else schema, "write_cache",
                        lambda p, d: False)
    schema_cmd.run(["--dev"])
    out, err = capsysbinary.readouterr()
    assert out == b'{"tables": ["fresh"]}\n'
    assert b"could not be cached" in err
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k run_writes -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db_core.commands.schema'`

**Step 3: Write minimal implementation**

Create `db_core/commands/schema.py`:

```python
"""The `schema` command: emit a database's full schema as JSON, cached.

Built for an external tool (auto-complete, linting, option hints, UI search)
that loads the document once per refresh and re-indexes it into its own
structure — so the whole document is served, always, and there are no
projection flags to slice it.

Adds NO disclosure surface: anyone who can run a query here can already read
`information_schema` and `pg_catalog` directly. This is a convenience wrapper
over statements the caller is already authorized to run.
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
    name = app.current().name
    parser = argparse.ArgumentParser(
        prog=f"{name} schema",
        description=(
            "Print a complete JSON description of an environment's schema:\n"
            "tables, views, columns, constraints, indexes, enums, domains,\n"
            "functions, sequences, triggers, and comments.\n\n"
            "The result is cached, so repeated calls do not re-introspect.\n"
            "Only the JSON goes to stdout, so it pipes straight into a parser."
        ),
        epilog="examples:\n"
               f"  {name} schema --dev > schema.json\n"
               f"  {name} schema --dev --refresh          # after a migration\n"
               f"  {name} schema --dev --max-age 1h       # accept an older cache\n"
               f"  {name} schema --token <TOKEN> --meta   # unattended, with status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN",
                       help="use an ephemeral access token instead of an environment")
    parser.add_argument("--refresh", action="store_true",
                        help="re-introspect now, ignoring any cached copy")
    parser.add_argument("--max-age", metavar="AGE", default=None,
                        help="serve a cached copy only if younger than AGE "
                             f"(45s/30m/2h/1d; default "
                             f"{schema.DEFAULT_MAX_AGE_SECONDS // 60}m, 0 to bypass)")
    parser.add_argument("--meta", action="store_true",
                        help="report cache status (cached/refreshed) on stderr")
    return parser


def parse_max_age(text: "str | None") -> float:
    """`--max-age` in seconds. Bare `0` means "bypass the cache".

    Not `tokens.parse_ttl`: that one forbids zero and caps at the system-mode
    token maximum, neither of which applies to a cache lifetime.
    """
    if text is None:
        return schema.DEFAULT_MAX_AGE_SECONDS
    if text == "0":
        return 0
    return tokens.parse_duration(text, "--max-age")


def _age_text(seconds: float) -> str:
    return f"{int(seconds)}s" if seconds < 60 else f"{int(seconds // 60)}m"


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             f"`{app.current().name} config set <name>`.")
    parser = build_parser(envs)
    args = parser.parse_args(argv)

    if args.token:
        database_url = tokens.load_database_url_from_token(args.token)
    else:
        database_url = store.load_database_url(selected_env(args, envs))

    try:
        result = schema.load(database_url, max_age=parse_max_age(args.max_age),
                             refresh=args.refresh)
    except Exception as e:
        # Same split as the exec path (see commands/exec.py and
        # query.server_error): a server-side error only describes the caller's
        # own statement, but a connection error can echo host/user/dbname.
        if in_system_mode():
            detail = query.server_error(e)
            fail(f"Schema introspection failed: {detail}" if detail
                 else "Schema introspection failed")
        print(f"Schema introspection failed: {e}", file=sys.stderr)
        sys.exit(1)

    # A byte copy, never print(): the document is megabytes, and stdout must
    # carry nothing but the JSON.
    sys.stdout.buffer.write(result.document)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()

    if not result.cache_written and not result.cached:
        print("Warning: the schema could not be cached; the next call will "
              "introspect again.", file=sys.stderr)
    if args.meta:
        if result.cached:
            print(f"cached (age {_age_text(result.age)})", file=sys.stderr)
        else:
            print(f"refreshed in {result.elapsed:.1f}s", file=sys.stderr)
```

**Known issue to resolve in this task — a masked `pgcode` silently withholds a real server error.**

Surfaced during Task 5's review. `introspect` ends its transaction in a `finally`, so when the server terminates the backend mid-query, `cur.execute` raises `OperationalError` (which *has* a SQLSTATE) but the subsequent `conn.rollback()` raises `InterfaceError` — and *that* is what propagates. The original survives only as `__context__`.

`query.server_error()` (`core/query.py:80`) keys disclosure off `getattr(exc, "pgcode", None)`, and the masking `InterfaceError` has `pgcode = None`. So in hardened mode this path reports a bare "Schema introspection failed" even though the server did explain itself. `run_query` has the identical masking, so this is a pre-existing repo convention rather than a regression — but the exec path's whole point (commit `976a085`) is that a server-side complaint is safe to disclose.

Decide explicitly: either walk `__context__` when the top-level exception has no `pgcode`, or accept the convention and say so in a comment. Do not leave it undecided.

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schema.py -v`
Expected: PASS. Note the `monkeypatch.setattr(schema.schema_core if False else schema, ...)` line in the test above is a typo — patch `db_core.core.schema.write_cache` (the `schema` imported from `db_core.core`). Clean it up while making it pass.

**Step 5: Commit**

```bash
git add db_core/commands/schema.py tests/test_schema.py
git commit -m "feat: add the schema command"
```

---

## Task 9: Wire `schema` into the CLI

**Files:**
- Modify: `db_core/cli.py:14` (import), `db_core/cli.py:19-58` (help), `db_core/cli.py:112-117` (dispatch)
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
def test_cli_dispatches_schema(monkeypatch):
    from db_core import cli
    seen = []
    monkeypatch.setattr(cli.schema_cmd, "run", lambda argv: seen.append(argv))
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev"])
    monkeypatch.setattr(cli.tokens, "sweep_expired", lambda: [])
    monkeypatch.setattr(cli, "maybe_redirect_to_launcher", lambda: None)
    cli.main()
    assert seen == [["--dev"]]


def test_top_level_help_mentions_schema(capsys):
    from db_core import cli
    cli.print_top_level_help()
    assert "schema" in capsys.readouterr().out
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k cli_dispatches -v`
Expected: FAIL — `AttributeError: module 'db_core.cli' has no attribute 'schema_cmd'`

**Step 3: Write minimal implementation**

`db_core/cli.py:14`:

```python
from .commands import config, password, token
from .commands import exec as exec_cmd
from .commands import schema as schema_cmd
```

`db_core/cli.py:112-117` — add the branch:

```python
    if argv[0] == "password":
        password.run(argv[1:])
    elif argv[0] == "token":
        token.run(argv[1:])
    elif argv[0] == "schema":
        schema_cmd.run(argv[1:])
    else:
        exec_cmd.run(argv)
```

In `TOP_LEVEL_HELP`, insert after the "Output formats" block:

```
Dump the schema as JSON (for editors, linters, and other tools):
  {name} schema --dev              full schema: tables, columns, keys, enums, ...
  {name} schema --dev --refresh    re-read it now (e.g. after a migration)
  Cached for 15m by default; only JSON goes to stdout.
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/cli.py tests/test_schema.py
git commit -m "feat: dispatch and document the schema command"
```

---

## Task 10: `config rm` clears the schema cache

**Files:**
- Modify: `db_core/commands/config.py:12` (import), `db_core/commands/config.py:77-87` (`cmd_rm`)
- Test: `tests/test_schema.py`

**Step 1: Write the failing test**

```python
def test_config_rm_clears_the_schema_cache(dev_env, capsys):
    from db_core.commands import config as config_cmd
    schema.write_cache(schema.cache_path(URL_A), b"{}")
    config_cmd.cmd_rm("dev")
    assert list(schema.cache_dir().glob("*.json")) == []
    assert "cached schema" in capsys.readouterr().out
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py -k config_rm -v`
Expected: FAIL — the cache file still exists.

**Step 3: Write minimal implementation**

`db_core/commands/config.py:12`:

```python
from ..core import crypto, schema, store, tokens
```

Append to `cmd_rm` (after the `revoked` block):

```python
    cleared = schema.clear_cache()
    if cleared:
        # Entries are keyed by URL hash, so `rm` cannot pick out "its" entry
        # without decrypting the env first. The cache is cheap to regenerate.
        print(f"Cleared {cleared} cached schema document(s).")
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db_core/commands/config.py tests/test_schema.py
git commit -m "feat: clear the schema cache on config rm"
```

---

## Task 11: Integration test against a real database

The unit tests fake psycopg2, so **nothing has yet proven the SQL parses**. This is the task that does. It stays skipped by default so CI needs no database.

**Files:**
- Create: `tests/test_schema_integration.py`

**Step 1: Write the test**

```python
"""Exercises the catalog SQL against a real server.

Skipped unless EXECUTE_DB_TEST_URL is set, e.g.:
    EXECUTE_DB_TEST_URL="$(explore-db --dev ...)" python -m pytest \
        tests/test_schema_integration.py -v

The unit tests fake psycopg2, so this is the only thing that proves
INTROSPECT_SQL actually parses and returns the documented shape.
"""

import json
import os

import pytest

from db_core.core import schema

URL = os.environ.get("EXECUTE_DB_TEST_URL")

pytestmark = pytest.mark.skipif(not URL, reason="EXECUTE_DB_TEST_URL not set")


def test_introspect_returns_the_documented_shape():
    doc = json.loads(schema.introspect(URL))
    assert doc["schema_version"] == schema.SCHEMA_VERSION
    for key in ("generated_at", "database", "server_version", "schemas",
                "tables", "enums", "domains", "functions", "sequences",
                "extensions"):
        assert key in doc, f"missing top-level key: {key}"
    assert isinstance(doc["tables"], list)


def test_tables_carry_columns_and_keys():
    doc = json.loads(schema.introspect(URL))
    table = next(t for t in doc["tables"] if t["kind"] == "table" and t["columns"])
    col = table["columns"][0]
    for key in ("name", "type", "not_null", "default", "identity", "generated",
                "position", "comment"):
        assert key in col, f"missing column key: {key}"   # nulls are KEPT
    for key in ("schema", "name", "kind", "comment", "columns", "constraints",
                "indexes", "triggers"):
        assert key in table


def test_foreign_keys_are_structured_not_just_text():
    doc = json.loads(schema.introspect(URL))
    fks = [c for t in doc["tables"] for c in t["constraints"]
           if c["type"] == "foreign_key"]
    if not fks:
        pytest.skip("no foreign keys in this database")
    fk = fks[0]
    assert fk["references"]["table"]
    assert fk["references"]["columns"]      # join hints without parsing DDL


def test_document_is_not_pretty_printed():
    raw = schema.introspect(URL)
    assert b"\n    " not in raw[:200]       # compact: no jsonb_pretty
```

**Step 2: Run it**

Run: `EXECUTE_DB_TEST_URL='postgresql://...' python -m pytest tests/test_schema_integration.py -v`
Expected: PASS. **If it fails with `IndexError: unsupported format character`, the `%` doubling from Task 5's note was missed.**

Run without the variable to confirm it skips: `python -m pytest tests/ -v` → 4 skipped.

**Step 3: Commit**

```bash
git add tests/test_schema_integration.py
git commit -m "test: integration coverage for the catalog SQL"
```

---

## Task 12: Verify against the real dev database

Not a test — a real end-to-end run. @verify

**Step 1: Install and run**

```bash
pip install -e .
explore-db schema --dev --meta > /tmp/schema.json
```

Expected on stderr: `refreshed in 2.4s` (roughly 2-4s).

**Step 2: Check the payload**

```bash
wc -c /tmp/schema.json          # expect ~11MB
python -c "import json;d=json.load(open('/tmp/schema.json'));print(len(d['tables']),'tables',sum(len(t['columns']) for t in d['tables']),'columns')"
```

Expected: ~2,127 tables and ~31,612 columns, matching the design doc's measurements.

**Step 3: Check the cache actually hits**

```bash
time explore-db schema --dev --meta > /tmp/schema2.json
```

Expected on stderr: `cached (age 0m)`, and the run should take well under a second — this is the byte-copy path.

```bash
cmp /tmp/schema.json /tmp/schema2.json && echo "identical"
ls -la ~/.explore-db/cache/
```

Expected: identical, and one `<hash>.v1.json` file at mode 600.

**Step 4: Check `--refresh` and hardened-mode disclosure**

```bash
explore-db schema --dev --refresh --meta > /dev/null     # expect: refreshed in Ns
explore-db schema --dev --max-age 0 --meta > /dev/null   # expect: refreshed in Ns
```

**Step 5: Commit anything the verification turned up**

---

## Task 13: Document it in the README

**Files:**
- Modify: `README.md`

Add a `schema` section covering: what it emits, the 15m TTL, `--refresh` after a migration, `--token` for unattended tools, that stdout is pure JSON (so `> schema.json` and pipes work), that the cache lives in the config dir keyed by URL hash, and that `config rm` clears it. Mention it works identically in `explore-db`, which is the natural home for it.

```bash
git add README.md
git commit -m "docs: document the schema command"
```

---

## Definition of done

- [ ] `python -m pytest tests/ -v` passes; integration tests skip cleanly without `EXECUTE_DB_TEST_URL`.
- [ ] `EXECUTE_DB_TEST_URL=... python -m pytest tests/test_schema_integration.py` passes against a real database.
- [ ] `explore-db schema --dev` emits ~11MB of valid JSON in ~2-4s; a second call serves from cache in well under a second.
- [ ] `execute-db schema --dev` works identically and still connects read-only.
- [ ] Nothing but JSON on stdout — `explore-db schema --dev | python -m json.tool > /dev/null` succeeds.
- [ ] `config rm` clears the cache.
- [ ] README documents the command.
