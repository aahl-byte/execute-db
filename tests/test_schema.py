"""The `schema` command: introspection, caching, and the CLI surface."""

import os
import re
import time

import pytest

from db_core import app
from db_core.core import schema, system, tokens

URL_A = "postgresql://u:p@host/db_a"
URL_B = "postgresql://u:p@host/db_b"


def test_parse_duration_units():
    assert tokens.parse_duration("45s", "--max-age") == 45
    assert tokens.parse_duration("30m", "--max-age") == 1800
    assert tokens.parse_duration("2h", "--max-age") == 7200
    assert tokens.parse_duration("1d", "--max-age") == 86400


def test_parse_duration_allows_zero_unlike_parse_ttl():
    # --max-age 0 means "bypass the cache"; --ttl 0 is still nonsense.
    assert tokens.parse_duration("0s", "--max-age") == 0
    with pytest.raises(SystemExit):
        tokens.parse_ttl("0s")


def test_parse_duration_ignores_the_system_ttl_cap(monkeypatch):
    # A cache lifetime is not a credential lifetime: the hardened-mode cap that
    # bounds --ttl has no bearing on --max-age.
    monkeypatch.setattr(system, "in_system_mode", lambda: True)
    assert tokens.parse_duration("48h", "--max-age") == 172800
    with pytest.raises(SystemExit):
        tokens.parse_ttl("48h")


def test_parse_duration_names_the_flag_in_its_error(capsys):
    with pytest.raises(SystemExit):
        tokens.parse_duration("soon", flag="--max-age")
    assert "--max-age" in capsys.readouterr().err


def test_cache_key_is_stable_and_url_specific():
    assert schema.cache_key(URL_A) == schema.cache_key(URL_A)
    assert schema.cache_key(URL_A) != schema.cache_key(URL_B)
    assert len(schema.cache_key(URL_A)) == 12


def test_cache_path_never_contains_the_url(store):
    path = schema.cache_path(URL_A)
    assert not any(p in str(path) for p in ("host", "p@", "db_a", "postgresql"))
    assert path.parent == store / "cache"


def test_cache_path_is_a_digest_and_a_version_and_nothing_else(store):
    # Asserted structurally, not by rebuilding the implementation's f-string:
    # any extra component (an env name, a db name added "for debuggability")
    # fails here even though the substring check above cannot know to look for it.
    assert re.fullmatch(r"[0-9a-f]{12}\.v\d+\.json", schema.cache_path(URL_A).name)


def test_a_schema_version_bump_misses_the_old_cache(store, monkeypatch):
    old = schema.cache_path(URL_A)
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1)
    assert schema.cache_path(URL_A) != old


def test_cache_age_reports_the_mtime_and_nothing_for_an_absent_file(store):
    path = schema.cache_path(URL_A)
    assert schema.cache_age(path) is None
    schema.write_cache(path, b"{}")
    assert 0 <= schema.cache_age(path) < 5
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert 3595 < schema.cache_age(path) < 3605


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


def test_write_never_exposes_a_partial_document(store, monkeypatch):
    # The tmp-plus-replace dance is the whole reason write_cache is not a bare
    # write_bytes, but "no tmp left behind" passes for a direct write too. So
    # hook the write and ask what a concurrent reader would see at that instant:
    # under a torn write it is the half-written document, and serving that as
    # truth is exactly what the dance exists to prevent.
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b'{"old": true}')
    real_write_bytes = schema.Path.write_bytes
    seen = []

    def spy(self, data):
        result = real_write_bytes(self, data)
        seen.append(path.read_bytes())
        return result

    monkeypatch.setattr(schema.Path, "write_bytes", spy)
    assert schema.write_cache(path, b'{"new": true}') is True
    assert seen == [b'{"old": true}']  # the new bytes landed somewhere else first
    assert path.read_bytes() == b'{"new": true}'


def test_read_cache_misses_when_stale(store):
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b"{}")
    old = time.time() - 3600
    os.utime(path, (old, old))  # mtime IS the fetch time
    assert schema.read_cache(path, max_age=60) is None
    assert schema.read_cache(path, max_age=7200) == b"{}"


def test_read_cache_misses_when_the_clock_jumped_backwards(store):
    # A future mtime means a skewed clock, not a fresh document. Read as an age
    # it goes negative, and a negative age is "younger than max_age" for every
    # max_age -- pinning the entry as fresh forever, which silently defeats the
    # cache's one contract. A spurious miss costs one re-introspection.
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b"{}")
    future = time.time() + 3600
    os.utime(path, (future, future))
    assert schema.read_cache(path, max_age=60) is None
    assert schema.read_cache(path, max_age=86400) is None


def test_read_cache_misses_when_absent_or_empty(store):
    path = schema.cache_path(URL_A)
    assert schema.read_cache(path, max_age=60) is None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(b"")  # truncated/corrupt -> treat as a miss
    assert schema.read_cache(path, max_age=60) is None


def test_read_cache_misses_when_the_entry_cannot_be_read(store):
    # The "unreadable file is a miss" half of the promise: a directory at the
    # cache path stats fine but read_bytes raises. A real kernel error, so this
    # holds as root, where a chmod-based unreadable file would not.
    path = schema.cache_path(URL_A)
    path.mkdir(mode=0o700, parents=True)
    assert schema.read_cache(path, max_age=60) is None


def test_write_cache_reports_failure_instead_of_raising(store):
    # Provoke a real OSError from the real filesystem rather than monkeypatching
    # Path.mkdir: a regular file occupies the cache dir's name, so mkdir raises
    # FileExistsError despite exist_ok=True (which forgives only a directory).
    # Unlike a chmod-based test this still fails to write when run as root.
    schema.cache_dir().write_bytes(b"not a directory")
    assert schema.write_cache(schema.cache_path(URL_A), b"{}") is False


def test_write_cache_creates_the_dir_of_the_path_it_was_handed(store):
    # write_cache takes a path, so it must create *that* path's parent rather
    # than reaching back to cache_dir() -- which would fail for any other path
    # and leave a spurious cache/ dir behind as a souvenir.
    path = store / "elsewhere" / "doc.json"
    assert schema.write_cache(path, b"{}") is True
    assert path.read_bytes() == b"{}"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not schema.cache_dir().exists()


def test_clear_cache_removes_every_entry(store):
    schema.write_cache(schema.cache_path(URL_A), b"{}")
    schema.write_cache(schema.cache_path(URL_B), b"{}")
    assert schema.clear_cache() == 2
    assert schema.clear_cache() == 0  # idempotent, and no dir is fine


def test_clear_cache_counts_only_what_it_actually_removed(store):
    # An entry that will not unlink (here a directory wearing a .json name) is
    # skipped rather than raised, and must not be counted as reclaimed.
    schema.write_cache(schema.cache_path(URL_A), b"{}")
    stubborn = schema.cache_dir() / "bogus.v1.json"
    stubborn.mkdir()
    assert schema.clear_cache() == 1
    assert stubborn.is_dir()


# --- introspect --------------------------------------------------------------
#
# These fake psycopg2 (the house idiom, from tests/test_explore.py) and so prove
# the CONNECTION CONTRACT, not the SQL. The SQL itself is exercised against a
# real database by the integration test.

class _FakeSchemaCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sql = sql
        self.args = args

    def fetchone(self):
        return ('{"tables": []}',)  # psycopg2 hands back str for a ::text column


class _FakeSchemaConn:
    def __init__(self):
        self.cur = _FakeSchemaCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

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
    assert captured_schema_connect["url"] == URL_A


def test_introspect_is_read_only_even_under_execute_db(captured_schema_connect):
    # The execute-db spec is read/write (conftest configures it), but
    # introspection must be structurally incapable of writing regardless.
    assert app.current().read_only is False
    schema.introspect(URL_A)
    # Asserted whole, as tests/test_explore.py does: an `in` check survives
    # dropping the `-c ` prefix that libpq needs to accept the setting at all.
    assert captured_schema_connect["kwargs"]["options"] == "-c default_transaction_read_only=on"
    assert captured_schema_connect["kwargs"]["sslmode"] == "require"


def test_introspect_closes_the_connection(captured_schema_connect):
    schema.introspect(URL_A)
    assert captured_schema_connect["conn"].closed is True


def test_introspect_closes_the_connection_even_when_the_query_fails(captured_schema_connect, monkeypatch):
    # The finally is the whole point: a catalog query that raises must not leak
    # the connection (and its read snapshot) for the rest of the process.
    def boom(sql, args=None):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(_FakeSchemaCursor, "execute", staticmethod(boom))
    with pytest.raises(RuntimeError):
        schema.introspect(URL_A)
    # Both, not just closed: the aborted transaction is ended here explicitly
    # rather than left to close()'s implicit rollback.
    assert captured_schema_connect["conn"].rolled_back is True
    assert captured_schema_connect["conn"].closed is True


def test_introspect_closes_the_connection_even_when_the_rollback_itself_fails(
    captured_schema_connect, monkeypatch
):
    # A terminated backend fails execute() AND rollback(). The other error-path
    # test cannot see this: its fake rolls back cleanly, so a throwing rollback
    # sitting ahead of close() would strand the connection with the suite green.
    def boom(sql, args=None):
        raise RuntimeError("backend terminated")

    def bad_rollback(self):
        raise RuntimeError("connection already closed")

    monkeypatch.setattr(_FakeSchemaCursor, "execute", staticmethod(boom))
    monkeypatch.setattr(_FakeSchemaConn, "rollback", bad_rollback)
    with pytest.raises(RuntimeError):
        schema.introspect(URL_A)
    assert captured_schema_connect["conn"].closed is True


def test_introspect_passes_the_schema_version_as_a_parameter(captured_schema_connect):
    # The document must declare the version this code produces, so it is bound
    # from the constant rather than baked into the SQL where a bump could miss it.
    schema.introspect(URL_A)
    assert captured_schema_connect["conn"].cur.args == {"schema_version": schema.SCHEMA_VERSION}


def test_introspect_does_not_hold_the_snapshot_open(captured_schema_connect):
    # A read-only transaction has nothing to commit; end it explicitly rather
    # than leaving the snapshot pinned until close() gets around to it.
    schema.introspect(URL_A)
    assert captured_schema_connect["conn"].rolled_back is True
    assert captured_schema_connect["conn"].committed is False


def test_introspect_casts_to_text_so_psycopg2_does_not_parse_it():
    # Without ::text psycopg2 parses jsonb into a dict and the raw-bytes cache
    # (and its no-parse cache hit) is silently defeated.
    #
    # Pinned to the TERMINAL cast, not a bare `"::text" in ...`: the SQL casts
    # to text elsewhere too (contype::text), so a substring check passes while
    # the one cast the design rests on is gone.
    assert schema.INTROSPECT_SQL.rstrip().endswith(")::text AS schema")


def test_introspect_refuses_to_hand_back_a_parsed_dict(captured_schema_connect, monkeypatch):
    # The other half of the ::text contract. If the cast were ever dropped,
    # psycopg2 yields a dict; introspect must fail loudly at the cause rather
    # than pass it through and break its own `-> bytes` contract downstream.
    monkeypatch.setattr(_FakeSchemaCursor, "fetchone", lambda self: ({"tables": []},))
    with pytest.raises(AttributeError):
        schema.introspect(URL_A)


def test_introspect_sql_survives_psycopg2_percent_formatting():
    # psycopg2 %-formats the query when args are passed, so every literal % in
    # a LIKE pattern must be doubled. This is the cheap, DB-free fence for it.
    assert schema.INTROSPECT_SQL % {"schema_version": 1}


def test_introspect_document_declares_every_top_level_key():
    # The loader is a strict typed reader: a key that quietly stops being built
    # is a break for it, and the fake cursor above cannot notice.
    for key in (
        "schema_version", "generated_at", "database", "server_version",
        "schemas", "tables", "enums", "domains", "functions", "sequences",
        "extensions",
    ):
        assert f"'{key}'," in schema.INTROSPECT_SQL
