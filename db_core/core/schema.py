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


@dataclass
class SchemaResult:
    """A document, plus how it was come by.

    `cached` is the one thing to switch on; the rest is detail for `--meta` to
    print. `elapsed` and `cache_written` are meaningful in exactly one branch --
    a cache hit introspected nothing to time and wrote nothing -- and default to
    None outside it, so None reads as "not that branch" rather than as a value.
    `cache_written=False` therefore means an attempt that FAILED, never "no
    attempt": a caller warning on a failed write wants `is False`, which a hit's
    None must not trip.

    `age` is the exception: it is best-effort even on a hit (see load), so
    `age is None` does NOT mean "not cached" -- only `cached` answers that.
    """

    document: bytes  # raw JSON, exactly as Postgres produced it
    cached: bool  # served from disk without connecting?
    # seconds since fetch, when cached; None if it could not be stat'd
    age: "float | None" = None
    elapsed: "float | None" = None  # seconds spent introspecting, when not cached
    # write succeeded? None when none was ATTEMPTED (cache hit)
    cache_written: "bool | None" = None


def cache_key(database_url: str) -> str:
    return hashlib.sha256(database_url.encode()).hexdigest()[:12]


def cache_dir() -> Path:
    return store.config_dir() / "cache"


def cache_path(database_url: str) -> Path:
    return cache_dir() / f"{cache_key(database_url)}.v{SCHEMA_VERSION}.json"


def cache_age(path: Path) -> "float | None":
    """Seconds since the document was fetched, or None if it cannot be stat'd.

    The file's mtime IS the fetch time; there is deliberately no metadata
    sidecar and no `fetched_at` field to keep in sync with it.

    A missing entry and an unreachable one (EACCES, ELOOP) are deliberately the
    same answer: both mean "no usable document here", and the caller's move is
    identical either way.
    """
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def read_cache(path: Path, max_age: float) -> "bytes | None":
    """The cached document if it is no older than `max_age`, else None.

    `max_age` is a freshness bound, not a sentinel: bypassing the cache is the
    caller's control flow, not `max_age=0`.

    Any unreadable or empty file is a miss, never an error: a truncated cache
    must degrade into a re-introspection, not a failure.
    """
    age = cache_age(path)
    # A future mtime means a skewed clock, not a fresh document: a negative age
    # is younger than every max_age, so it would pin the entry as fresh until
    # the clock caught up, defeating max_age entirely. Miss instead -- that
    # costs one re-introspection, the same degrade path a corrupt file takes.
    if age is None or not 0 <= age <= max_age:
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
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    cache_d = cache_dir()
    if not cache_d.is_dir():
        return 0
    count = 0
    for p in cache_d.glob("*.json"):
        try:
            p.unlink()
            count += 1
        except OSError:
            pass
    return count


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
           -- (nspname, proname) is not unique: overloads tie, and a tie orders
           -- arbitrarily, so an unchanged schema can produce different bytes on
           -- every refresh. Broken by identity arguments -- a key drawn from the
           -- document's own visible content rather than an internal oid -- which
           -- (proname, proargtypes, pronamespace) being uniquely indexed makes
           -- total.
           ) ORDER BY n.nspname, p.proname,
                      pg_get_function_identity_arguments(p.oid)) AS j
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      -- extension-owned functions (PostGIS, pg_trgm, ...) are thousands of
      -- entries nobody is completing against.
      --
      -- classid is load-bearing: oids are unique per catalog, not globally, so
      -- an unqualified objid = p.oid can match a row describing some other
      -- catalog's object that happens to share the number -- silently dropping
      -- a real function from auto-complete.
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_proc'::regclass
                        AND d.objid = p.oid AND d.deptype = 'e')
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
        sslmode="require",  # the same posture core.query connects with
        options="-c default_transaction_read_only=on",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(INTROSPECT_SQL, {"schema_version": SCHEMA_VERSION})
            # Exactly one row by construction: the final SELECT has no top-level
            # FROM and reaches every CTE through a scalar subquery, so it yields
            # one row even against an empty database. fetchone() cannot be None.
            (document,) = cur.fetchone()
    finally:
        # Nothing to commit on either path, and no reason to hold the snapshot
        # open while an 11MB string is encoded below. Not commit(): this
        # transaction is structurally incapable of change, so claiming there is
        # work to keep would be a lie -- core.query commits only because it is
        # one flow for reads AND writes. In the finally, not on the success path:
        # relying on close()'s implicit rollback is a psycopg2 disposition detail
        # that stops being true the day this connection comes from a pool, and
        # the error path -- where the transaction is aborted -- is exactly where
        # a pool would care most.
        try:
            conn.rollback()
        finally:
            # A terminated backend makes rollback() raise too, and an unguarded
            # throwing rollback ahead of close() would strand the connection
            # entirely -- the opposite of what the rollback is here to protect.
            conn.close()
    # No isinstance(document, str) guard: `::text` makes this a text column (oid
    # 25), which psycopg2 always decodes with its STRING typecaster. The guard's
    # only reachable cause would be someone dropping the cast -- and then
    # psycopg2 yields a dict, which the guard would hand back as-is, silently
    # breaking the `-> bytes` contract. Failing loudly here, at the cause, beats
    # a dict escaping into the cache.
    return document.encode()


def load(database_url: str, max_age: float = DEFAULT_MAX_AGE_SECONDS,
         refresh: bool = False) -> SchemaResult:
    """The document for `database_url`, from cache when fresh enough.

    `refresh` and `max_age=0` both mean "do not serve me a cached document";
    either way the fresh one is still cached on the way out, so bypassing the
    read never costs the NEXT caller a connection.

    Introspection failures are raised, not caught: the caller decides how much
    of a database error it may disclose, and it cannot decide that about an
    exception this function has already swallowed. Nothing is written until the
    document is in hand, so a failure leaves the previous entry untouched.
    """
    path = cache_path(database_url)
    # One decision, not two conditions: `refresh` and `max_age=0` are the same
    # idea to this function, so they collapse into the one question it asks.
    #
    # max_age=0 must not reach read_cache. There it would "work" only by
    # arithmetic accident -- `0 <= age <= 0` misses because an age is never
    # exactly 0.0 -- making bypass a property a reader has to derive rather than
    # read, and overloading max_age with a second job it explicitly disclaims
    # (see read_cache). Deciding it here also spares a pointless stat().
    if not refresh and max_age > 0:
        document = read_cache(path, max_age)
        if document is not None:
            # A second stat, on purpose: `age` exists only for `--meta` to print,
            # and one syscall is a better price than widening read_cache's
            # signature to carry a number its other callers do not want. The
            # price is that an entry cleared between the read and this stat
            # reports age=None -- honest, and a served document with an unknown
            # age beats failing over a number nothing depends on.
            return SchemaResult(document=document, cached=True, age=cache_age(path))

    # monotonic, not time(): this is a duration, and wall time can step under
    # NTP mid-introspection and report a refresh that took seconds as negative.
    # cache_age() reads time() because it compares against an mtime, which is
    # wall time by definition; nothing here has to agree with the filesystem.
    started = time.monotonic()
    document = introspect(database_url)
    # The introspection only -- the caller waits on the database, not on the
    # write, and the write is milliseconds against seconds. Timing both would
    # blame the disk for the query's cost, or the query for the disk's.
    elapsed = time.monotonic() - started
    return SchemaResult(document=document, cached=False, elapsed=elapsed,
                        cache_written=write_cache(path, document))
