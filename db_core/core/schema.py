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
from pathlib import Path

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
