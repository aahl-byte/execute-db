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
