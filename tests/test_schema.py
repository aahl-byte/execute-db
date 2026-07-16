"""The `schema` command: introspection, caching, and the CLI surface."""

import json
import os
import re
import time

import pytest

from db_core import app
from db_core.commands import schema as schema_cmd
from db_core.core import crypto, schema, system, tokens
from db_core.core import store as store_mod

from .conftest import ConnError as _ConnError
from .conftest import ServerError as _ServerError

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


# --- load --------------------------------------------------------------------


@pytest.fixture
def fake_introspect(monkeypatch):
    """Stand in for the database, and record every connection load makes."""
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
    assert result.elapsed is not None
    assert result.age is None  # a document just fetched has no age to report
    assert fake_introspect == [URL_A]
    assert schema.cache_path(URL_A).exists()


def test_load_hit_serves_cache_without_connecting(store, fake_introspect):
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    result = schema.load(URL_A)
    assert result.document == b'{"tables": ["cached"]}'
    assert result.cached is True
    assert result.age is not None
    assert result.elapsed is None  # nothing was introspected, so nothing to time
    # None, not False: nothing was ATTEMPTED. A caller warning on a failed cache
    # write spells that `is False`, and a hit must not trip it.
    assert result.cache_written is None
    assert fake_introspect == []  # never touched the database


def test_load_refresh_bypasses_a_fresh_cache(store, fake_introspect):
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    result = schema.load(URL_A, refresh=True)
    assert result.document == b'{"tables": ["fresh"]}'
    assert fake_introspect == [URL_A]
    # Bypassing the cache READ still refreshes the entry -- the half of "bypass"
    # that only prose defended. A bypass that skipped the write would leave the
    # stale document on disk for the NEXT caller to be served, which is the
    # opposite of what --refresh is for. Asserted on the file, not just the
    # flag: cache_written=True while the bytes stayed stale is the same bug.
    assert result.cache_written is True
    assert schema.cache_path(URL_A).read_bytes() == b'{"tables": ["fresh"]}'


def test_load_max_age_zero_always_introspects(store, fake_introspect):
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    result = schema.load(URL_A, max_age=0)
    assert result.document == b'{"tables": ["fresh"]}'
    # The same promise on the other bypass: max-age 0 is "do not SERVE me a
    # cached document", not "do not maintain the cache".
    assert result.cache_written is True
    assert schema.cache_path(URL_A).read_bytes() == b'{"tables": ["fresh"]}'


def test_load_bypass_is_control_flow_not_arithmetic(store, fake_introspect, monkeypatch):
    # The test above passes for the WRONG implementation. Handing max_age=0 to
    # read_cache also yields a miss -- but only because `0 <= age <= 0` demands
    # an age of exactly 0.0, i.e. by the arithmetic accident of time.time()
    # never equalling st_mtime. Ask the question that pins the design instead:
    # was the cache consulted at all? Both bypasses must answer no.
    schema.write_cache(schema.cache_path(URL_A), b'{"tables": ["cached"]}')
    consulted = []
    real_read_cache = schema.read_cache

    def spy(path, max_age):
        consulted.append(max_age)
        return real_read_cache(path, max_age)

    monkeypatch.setattr(schema, "read_cache", spy)
    assert schema.load(URL_A, max_age=0).document == b'{"tables": ["fresh"]}'
    assert consulted == []
    assert schema.load(URL_A, refresh=True).document == b'{"tables": ["fresh"]}'
    assert consulted == []


def test_load_stale_cache_reintrospects(store, fake_introspect):
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b'{"tables": ["cached"]}')
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert schema.load(URL_A, max_age=60).document == b'{"tables": ["fresh"]}'


def test_load_still_serves_when_the_cache_cannot_be_written(store, fake_introspect,
                                                            monkeypatch):
    monkeypatch.setattr(schema, "write_cache", lambda p, d: False)
    result = schema.load(URL_A)
    assert result.document == b'{"tables": ["fresh"]}'  # serving is the job
    assert result.cache_written is False


class _JumpingClock:
    """A wall clock stepping backwards, with the real monotonic clock beside it."""

    def __init__(self):
        self._wall = 5000.0

    def time(self):
        self._wall -= 1000.0  # every read lands further in the past
        return self._wall

    def monotonic(self):
        return time.monotonic()


def test_load_measures_elapsed_on_a_clock_that_cannot_jump(store, fake_introspect,
                                                           monkeypatch):
    # elapsed is a DURATION, so it comes off the monotonic clock. Wall time can
    # step backwards (NTP) mid-introspection and make a refresh that took two
    # seconds report as "refreshed in -1000.0s". This module already treats
    # clock skew as real -- read_cache misses on a future mtime for it -- so the
    # same skew must not be able to reach --meta's timing either.
    monkeypatch.setattr(schema, "time", _JumpingClock())
    result = schema.load(URL_A)
    assert result.elapsed >= 0


def test_load_leaves_the_cache_untouched_when_introspection_fails(store, monkeypatch):
    # load deliberately lets introspect's exceptions fly -- the command layer
    # owns the disclosure rules. What load still owes is that a failed refresh
    # destroys nothing: the previous entry survives for the next run to serve,
    # and no half-written document is left where it would be served as truth.
    path = schema.cache_path(URL_A)
    schema.write_cache(path, b'{"tables": ["cached"]}')
    old = time.time() - 3600
    os.utime(path, (old, old))  # stale, so load will try to refresh it

    def boom(url):
        raise RuntimeError("could not translate host name")

    monkeypatch.setattr(schema, "introspect", boom)
    with pytest.raises(RuntimeError):
        schema.load(URL_A, max_age=60)
    assert path.read_bytes() == b'{"tables": ["cached"]}'
    assert list(schema.cache_dir().glob("*.tmp")) == []


# --- parse_max_age -----------------------------------------------------------

def test_parse_max_age_defaults_to_the_engine_default():
    # None is "the flag was not given", and the default it means lives in the
    # core module beside load()'s own -- not copied into argparse.
    assert schema_cmd.parse_max_age(None) == schema.DEFAULT_MAX_AGE_SECONDS


def test_parse_max_age_reads_the_duration_grammar():
    assert schema_cmd.parse_max_age("45s") == 45
    assert schema_cmd.parse_max_age("30m") == 1800
    assert schema_cmd.parse_max_age("1d") == 86400


def test_parse_max_age_accepts_a_bare_zero_and_a_zero_with_a_unit():
    # The grammar demands a unit, but zero has none that means anything
    # different. `0` is the spelling --help documents; `0s` falls out of the
    # grammar and must not be an error just because it is unusual.
    assert schema_cmd.parse_max_age("0") == 0
    assert schema_cmd.parse_max_age("0s") == 0


def test_parse_max_age_ignores_the_hardened_ttl_cap(monkeypatch):
    # A cache lifetime is not a credential lifetime. --ttl is capped in hardened
    # mode because a token is a key; a stale schema is only stale.
    monkeypatch.setattr(system, "in_system_mode", lambda: True)
    assert schema_cmd.parse_max_age("48h") == 172800


# --- _age_text ---------------------------------------------------------------

def test_age_text_reads_in_the_units_max_age_is_spelled_in():
    assert schema_cmd._age_text(0) == "0s"
    assert schema_cmd._age_text(45.7) == "45s"
    assert schema_cmd._age_text(60) == "1m"
    assert schema_cmd._age_text(3599) == "59m"
    assert schema_cmd._age_text(7200) == "2h"
    assert schema_cmd._age_text(90000) == "1d"


def test_age_text_says_unknown_rather_than_formatting_a_none():
    # age is best-effort even on a hit: load re-stats the file, and an entry
    # cleared in between leaves the age unknown while the document it already
    # read is still perfectly good (see core.schema.load). int(None) is a crash.
    assert schema_cmd._age_text(None) == "unknown"


# --- the command -------------------------------------------------------------

DEV_URL = "postgresql://u:p@host/dev"
DOC = b'{"tables": ["fresh"]}'


@pytest.fixture
def dev_env(store):
    (store / ".env.dev").write_text(f"DATABASE_URL={DEV_URL}\n")
    return store


@pytest.fixture
def load_calls(monkeypatch):
    """Record what the command asks the engine for. No cache, no database."""
    calls = []

    def _load(url, max_age=schema.DEFAULT_MAX_AGE_SECONDS, refresh=False):
        calls.append({"url": url, "max_age": max_age, "refresh": refresh})
        return schema.SchemaResult(document=DOC, cached=False, elapsed=0.1,
                                   cache_written=True)

    monkeypatch.setattr(schema, "load", _load)
    return calls


def test_the_document_goes_to_stdout_and_nothing_else_does(dev_env, fake_introspect,
                                                           capsysbinary):
    schema_cmd.run(["--dev"])
    out, err = capsysbinary.readouterr()
    # Byte equality, not `in`: the consumer pipes this straight into a parser,
    # so one extra byte on stdout -- a banner, a progress line, a stray print --
    # breaks it. Equality is what makes that a failure rather than a shrug.
    assert out == DOC + b"\n"
    assert json.loads(out) == {"tables": ["fresh"]}
    assert err == b""


def test_the_document_is_copied_verbatim_not_reparsed(dev_env, monkeypatch,
                                                      capsysbinary):
    # The cache holds the exact bytes Postgres produced and stdout is a byte
    # copy of them. A parse-and-re-dump would reorder keys, respace, and unescape
    # -- and cost seconds on an 11MB document. This document survives none of it.
    doc = b'{"b":1,"a":2,"t":"caf\\u00e9","z":[ ]}'
    monkeypatch.setattr(schema, "introspect", lambda url: doc)
    schema_cmd.run(["--dev"])
    assert capsysbinary.readouterr()[0] == doc + b"\n"


def test_a_second_call_serves_the_cache_and_the_bypasses_do_not(dev_env,
                                                                fake_introspect):
    schema_cmd.run(["--dev"])
    schema_cmd.run(["--dev"])
    assert fake_introspect == [DEV_URL]        # the hit never connected
    schema_cmd.run(["--dev", "--refresh"])
    schema_cmd.run(["--dev", "--max-age", "0"])
    assert fake_introspect == [DEV_URL] * 3    # both bypasses re-introspected


def test_every_flag_reaches_the_engine(dev_env, load_calls):
    # The seam itself, so a flag that is parsed but dropped on the floor fails
    # here rather than passing quietly by looking like the default.
    schema_cmd.run(["--dev"])
    schema_cmd.run(["--dev", "--refresh"])
    schema_cmd.run(["--dev", "--max-age", "30m"])
    schema_cmd.run(["--dev", "--max-age", "0"])
    assert load_calls == [
        {"url": DEV_URL, "max_age": schema.DEFAULT_MAX_AGE_SECONDS, "refresh": False},
        {"url": DEV_URL, "max_age": schema.DEFAULT_MAX_AGE_SECONDS, "refresh": True},
        {"url": DEV_URL, "max_age": 1800, "refresh": False},
        {"url": DEV_URL, "max_age": 0, "refresh": False},
    ]


def test_max_age_rejects_nonsense_and_names_the_flag(dev_env, load_calls, capsys):
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev", "--max-age", "soon"])
    assert "--max-age" in capsys.readouterr().err
    assert load_calls == []


def test_max_age_is_rejected_before_the_environment_is_opened(dev_env, monkeypatch):
    # `schema --prod --max-age soon` must not prompt for the prod password and
    # THEN reject the flag. Ordering alone buys this: parse_max_age runs before
    # anything reaches for a credential. (It exits 1 via console.fail, not 2 via
    # parser.error -- the same way `token --ttl` rejects a bad duration.)
    def never(env):
        raise AssertionError("opened the environment before validating --max-age")

    monkeypatch.setattr(store_mod, "load_database_url", never)
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev", "--max-age", "soon"])


# --- --meta ------------------------------------------------------------------

def test_meta_reports_a_refresh_with_the_time_it_took(dev_env, fake_introspect,
                                                      capsysbinary):
    schema_cmd.run(["--dev", "--meta"])
    out, err = capsysbinary.readouterr()
    assert re.fullmatch(rb"refreshed in \d+\.\d+s\n", err)
    assert out == DOC + b"\n"   # --meta must never contaminate stdout


def test_meta_reports_a_cache_hit_with_its_age(dev_env, fake_introspect,
                                               capsysbinary):
    schema_cmd.run(["--dev"])
    capsysbinary.readouterr()
    schema_cmd.run(["--dev", "--meta"])
    out, err = capsysbinary.readouterr()
    assert re.fullmatch(rb"cached \(age \d+s\)\n", err)
    assert out == DOC + b"\n"
    assert fake_introspect == [DEV_URL]


def test_meta_says_the_age_is_unknown_rather_than_none(dev_env, monkeypatch,
                                                       capsysbinary):
    # The age-unknown arm end to end: a hit whose file vanished before load
    # could stat it. The document is good, so it is still served in full.
    monkeypatch.setattr(schema, "load", lambda *a, **k: schema.SchemaResult(
        document=DOC, cached=True, age=None))
    schema_cmd.run(["--dev", "--meta"])
    out, err = capsysbinary.readouterr()
    assert err == b"cached (age unknown)\n"
    assert out == DOC + b"\n"


# --- the cache-write warning -------------------------------------------------

def test_a_failed_cache_write_warns_but_still_serves(dev_env, fake_introspect,
                                                     monkeypatch, capsysbinary):
    monkeypatch.setattr(schema, "write_cache", lambda p, d: False)
    schema_cmd.run(["--dev"])
    out, err = capsysbinary.readouterr()
    assert out == DOC + b"\n"   # serving is the job; caching is the optimization
    assert b"could not be cached" in err


def test_a_cache_hit_does_not_warn_about_a_write_it_never_attempted(
    dev_env, fake_introspect, capsysbinary
):
    # cache_written is a TRI-state: None means no write was ATTEMPTED. Spelling
    # the warning `if not result.cache_written` fires it on every cache hit --
    # the common path -- which is the surest way to train someone to ignore a
    # warning that only ever matters because it is rare.
    schema_cmd.run(["--dev"])
    capsysbinary.readouterr()
    schema_cmd.run(["--dev"])
    assert capsysbinary.readouterr()[1] == b""


# --- picking a target --------------------------------------------------------

def test_a_token_is_used_in_place_of_an_environment(dev_env, fake_introspect,
                                                    monkeypatch, capsysbinary):
    monkeypatch.setattr(tokens, "load_database_url_from_token",
                        lambda t: "postgresql://u:p@host/tokened" if t == "TOK" else None)
    schema_cmd.run(["--token", "TOK"])
    assert fake_introspect == ["postgresql://u:p@host/tokened"]
    assert capsysbinary.readouterr()[0] == DOC + b"\n"


def test_an_environment_and_a_token_are_mutually_exclusive(dev_env, load_calls):
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev", "--token", "TOK"])
    assert load_calls == []


def test_a_target_is_required(dev_env, load_calls):
    with pytest.raises(SystemExit):
        schema_cmd.run([])
    assert load_calls == []


def test_an_empty_store_guides_the_user(store, capsys):
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev"])
    assert "config set" in capsys.readouterr().err


def test_an_encrypted_env_without_a_tty_says_so_and_never_connects(
    store, fake_introspect, capsys
):
    # Note this does NOT pin where the try/except boundary sits: the store
    # signals through console.fail() -> SystemExit, which derives from
    # BaseException and so escapes `except Exception` wherever the boundary is.
    # What it pins is the message itself -- an unattended caller is told the env
    # is encrypted and what to do instead, rather than watching a connection be
    # attempted with no URL. The boundary is pinned by the test below.
    (store / ".env.dev").write_bytes(
        crypto.encrypt(b"DATABASE_URL=postgresql://u:p@host/dev\n", "pw"))
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev"])            # no TTY under pytest
    err = capsys.readouterr().err
    assert "encrypted" in err
    assert "token create" in err             # the way out, not just the refusal
    assert fake_introspect == []


def test_a_store_failure_is_not_relabelled_as_an_introspection_failure(
    dev_env, monkeypatch, capsys
):
    # THE boundary test. Resolving the URL sits outside the try on purpose, and
    # only a store failure that skips console.fail() can show it: an OSError out
    # of read_bytes(), or anything unexpected from dotenv/keyring. Swept inside
    # the try, this becomes "Schema introspection failed" -- and in hardened mode
    # is reduced to that bare string, stranding the caller with a lie about the
    # database and no idea their env file is unreadable.
    def boom(env):
        raise OSError("Permission denied: /home/execute-db/.execute-db/.env.dev")

    monkeypatch.setattr(store_mod, "load_database_url", boom)
    monkeypatch.setattr(schema_cmd, "in_system_mode", lambda: True)
    with pytest.raises(OSError):
        schema_cmd.run(["--dev"])
    assert capsys.readouterr().err != "Schema introspection failed\n"


# --- what a failure is allowed to say ----------------------------------------
#
# The rule itself lives in query.server_error and is pinned by
# tests/test_error_disclosure.py; its psycopg2 fakes live in tests/conftest.py.
# These pin that THIS command applies the rule, the same way the exec path does.

LEAKY = ('could not translate host name "db-internal.example" to address: '
         "Name or service not known")


def _introspection_raises(monkeypatch, exc):
    def boom(url):
        raise exc

    monkeypatch.setattr(schema, "introspect", boom)


def test_an_introspection_failure_exits_nonzero_and_prints_nothing_to_stdout(
    dev_env, monkeypatch, capsysbinary
):
    _introspection_raises(monkeypatch, _ConnError(LEAKY))
    with pytest.raises(SystemExit) as e:
        schema_cmd.run(["--dev"])
    assert e.value.code == 1
    out, err = capsysbinary.readouterr()
    assert out == b""            # a consumer redirecting to a file gets no half-document
    assert b"Schema introspection failed" in err


def test_outside_system_mode_the_whole_error_is_shown(dev_env, monkeypatch, capsys):
    # Not hardened: the caller owns the machine and the config, so withholding
    # the connection error would only hide their own typo from them.
    _introspection_raises(monkeypatch, _ConnError(LEAKY))
    monkeypatch.setattr(schema_cmd, "in_system_mode", lambda: False)
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev"])
    assert "db-internal.example" in capsys.readouterr().err


def test_system_mode_withholds_a_connection_error(dev_env, monkeypatch, capsys):
    # The leak the whole split exists to prevent: over sudo the caller may be an
    # agent that was never trusted with the host, user, or database name.
    _introspection_raises(monkeypatch, _ConnError(LEAKY))
    monkeypatch.setattr(schema_cmd, "in_system_mode", lambda: True)
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev"])
    err = capsys.readouterr().err
    # Exact, not `"db-internal" not in err`: naming the substring only catches
    # the leak I thought of. Equality catches every one.
    assert err == "Schema introspection failed\n"


def test_system_mode_discloses_a_server_error(dev_env, monkeypatch, capsys):
    _introspection_raises(monkeypatch,
                          _ServerError("42501", "permission denied for table pg_class"))
    monkeypatch.setattr(schema_cmd, "in_system_mode", lambda: True)
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev"])
    assert capsys.readouterr().err == (
        "Schema introspection failed: permission denied for table pg_class\n"
    )


def test_system_mode_discloses_a_server_error_masked_by_a_failed_rollback(
    dev_env, monkeypatch, capsys
):
    # The real shape of a terminated backend, end to end through this command:
    # introspect's finally rolls back, the rollback raises on the dead
    # connection, and THAT is what reaches here -- with the server's own words
    # only reachable through __context__. Reporting a bare "failed" while the
    # server sat there explaining itself is the failure this command must not
    # ship with. See query.server_error.
    original = _ServerError("57P01",
                            "terminating connection due to administrator command")
    masked = _ConnError("connection already closed")
    masked.__context__ = original
    _introspection_raises(monkeypatch, masked)
    monkeypatch.setattr(schema_cmd, "in_system_mode", lambda: True)
    with pytest.raises(SystemExit):
        schema_cmd.run(["--dev"])
    assert capsys.readouterr().err == (
        "Schema introspection failed: terminating connection due to "
        "administrator command\n"
    )


# --- browse subcommands: list / show / find ------------------------------------

def _rel(schema_, name, kind="table", columns=(), constraints=(), indexes=(),
         triggers=(), comment=None, view_definition=None):
    return {"schema": schema_, "name": name, "kind": kind, "comment": comment,
            "columns": list(columns), "constraints": list(constraints),
            "indexes": list(indexes), "triggers": list(triggers),
            "view_definition": view_definition}


def _col(name, type_="text", not_null=False, default=None, comment=None,
         identity=None, generated=None):
    return {"name": name, "type": type_, "not_null": not_null, "default": default,
            "identity": identity, "generated": generated, "comment": comment}


BROWSE_DOC = {
    "database": "shop",
    "schemas": ["public", "billing"],
    "tables": [
        _rel("public", "users", comment="app accounts",
             columns=[_col("id", "integer", not_null=True, default="nextval('s')"),
                      _col("email", "text", not_null=True, comment="login"),
                      _col("org_id", "integer")],
             constraints=[
                 {"name": "users_pkey", "type": "primary_key", "columns": ["id"],
                  "definition": "PRIMARY KEY (id)", "references": None},
                 {"name": "users_org_fk", "type": "foreign_key", "columns": ["org_id"],
                  "definition": "FOREIGN KEY (org_id) REFERENCES public.orgs(id)",
                  "references": {"table": "public.orgs", "columns": ["id"]}},
             ],
             indexes=[{"name": "users_pkey", "definition": "CREATE UNIQUE INDEX ...",
                       "unique": True, "primary": True}],
             triggers=[{"name": "users_audit", "definition": "AFTER INSERT ..."}]),
        _rel("public", "orgs", columns=[_col("id", "integer", not_null=True)]),
        _rel("public", "user_view", kind="view",
             columns=[_col("id", "integer")], view_definition="SELECT id FROM users"),
        _rel("billing", "invoices", columns=[_col("id", "integer")]),
    ],
    "enums": [
        {"schema": "public", "name": "user_status",
         "values": ["active", "suspended"], "comment": None},
    ],
    "functions": [
        {"schema": "billing", "name": "charge", "kind": "function",
         "arguments": "amount numeric", "identity_arguments": "numeric",
         "arg_count": 1, "returns": "boolean", "language": "plpgsql",
         "definition": "CREATE FUNCTION billing.charge(numeric) ...",
         "comment": "bill a card"},
        {"schema": "billing", "name": "charge", "kind": "function",
         "arguments": "amount numeric, currency text",
         "identity_arguments": "numeric, text", "arg_count": 2,
         "returns": "boolean", "language": "plpgsql",
         "definition": "CREATE FUNCTION billing.charge(numeric, text) ...",
         "comment": None},
    ],
    "domains": [], "sequences": [], "extensions": [],
}


def test_render_schema_list_counts_by_kind():
    out = schema_cmd.render_schema_list(BROWSE_DOC)
    assert "2 schemas in shop" in out
    # public: 2 tables (users, orgs), 1 view, 0 functions
    assert re.search(r"public\s+2\s+1\s+0", out)
    # billing: 1 table, 0 views, 2 functions (the two overloads)
    assert re.search(r"billing\s+1\s+0\s+2", out)


def test_render_schema_contents_groups_objects():
    out = schema_cmd.render_schema_contents(BROWSE_DOC, "public")
    assert "tables (2):" in out and "views (1):" in out and "enums (1):" in out
    assert "user_status {active, suspended}" in out
    # a view is not counted among tables
    assert "user_view" in out.split("views")[1]


def test_render_schema_contents_rejects_unknown_schema_with_a_hint(capsys):
    with pytest.raises(SystemExit):
        schema_cmd.render_schema_contents(BROWSE_DOC, "publi")
    err = capsys.readouterr().err
    assert "No schema named 'publi'" in err and "Did you mean: public" in err


def test_render_show_relation_has_every_section():
    out = schema_cmd.render_show(BROWSE_DOC, "public.users")
    assert out.startswith("table public.users")
    assert "-- app accounts" in out          # table comment
    for section in ("columns (3):", "constraints (2):", "indexes (1):", "triggers (1):"):
        assert section in out
    assert "-- login" in out                 # column comment, re-attached after alignment
    assert "not null" in out and "default nextval('s')" in out
    assert "REFERENCES public.orgs(id)" in out


def test_render_show_resolves_a_bare_name_uniquely():
    # 'users' exists only in public, so a bare name is enough.
    assert schema_cmd.render_show(BROWSE_DOC, "users").startswith("table public.users")


def test_render_show_enum_lists_values():
    out = schema_cmd.render_show(BROWSE_DOC, "user_status")
    assert out.startswith("enum public.user_status")
    assert "active" in out and "suspended" in out


def test_render_show_function_lists_overloads():
    out = schema_cmd.render_show(BROWSE_DOC, "billing.charge")
    assert "2 overloads" in out
    assert "amount numeric, currency text" in out
    assert "returns boolean" in out


def test_render_show_function_includes_the_definition_body():
    out = schema_cmd.render_show(BROWSE_DOC, "billing.charge")
    assert "definition:" in out
    # both overloads' bodies, indented under the signature
    assert "CREATE FUNCTION billing.charge(numeric) ..." in out
    assert "CREATE FUNCTION billing.charge(numeric, text) ..." in out


def test_render_show_function_without_a_body_skips_definition():
    # an aggregate carries a null definition (pg_get_functiondef refuses it)
    doc = {**BROWSE_DOC, "functions": [
        {"schema": "public", "name": "my_agg", "kind": "aggregate",
         "arguments": "integer", "identity_arguments": "integer", "arg_count": 1,
         "returns": "integer", "language": "internal", "definition": None,
         "comment": None}]}
    out = schema_cmd.render_show(doc, "my_agg")
    assert "definition:" not in out
    assert out.startswith("function public.my_agg")


def test_func_summary_is_compact_by_arg_count():
    f = lambda n: {"name": "fn", "arg_count": n}
    assert schema_cmd._func_summary(f(0)) == "fn()"
    assert schema_cmd._func_summary(f(1)) == "fn(...)  # 1 arg"
    assert schema_cmd._func_summary(f(3)) == "fn(...)  # 3 args"


def test_render_schema_contents_lists_functions_compactly():
    # billing's two `charge` overloads collapse to compact lines, not full args.
    out = schema_cmd.render_schema_contents(BROWSE_DOC, "billing")
    assert "functions (2):" in out
    assert "charge(...)  # 1 arg" in out and "charge(...)  # 2 args" in out
    # the noisy full argument list stays out of the list view
    assert "amount numeric, currency text" not in out


def test_render_show_ambiguous_bare_name_lists_candidates(capsys):
    doc = {**BROWSE_DOC, "tables": BROWSE_DOC["tables"] + [_rel("billing", "users")]}
    with pytest.raises(SystemExit):
        schema_cmd.render_show(doc, "users")
    err = capsys.readouterr().err
    assert "billing.users" in err and "public.users" in err


def test_render_show_unknown_object_fails():
    with pytest.raises(SystemExit):
        schema_cmd.render_show(BROWSE_DOC, "public.nope")


def test_render_find_is_case_insensitive_and_grouped():
    out = schema_cmd.render_find(BROWSE_DOC, "USER")
    assert "tables/views (2):" in out          # users and user_view
    assert "enums (1):" in out                 # user_status
    assert "public.user_view" in out and "public.users" in out


def test_render_find_matches_enum_values():
    out = schema_cmd.render_find(BROWSE_DOC, "suspended")
    assert "enum values (1):" in out
    assert "public.user_status -> suspended" in out


def test_render_find_reports_nothing_when_empty():
    assert "No schema, table, column" in schema_cmd.render_find(BROWSE_DOC, "zzznope")


def test_render_find_caps_each_category_and_says_so():
    cols = [_col(f"c{i}") for i in range(schema_cmd.FIND_CAP + 5)]
    doc = {**BROWSE_DOC, "tables": [_rel("public", "wide", columns=cols)],
           "enums": [], "functions": [], "schemas": ["public"]}
    out = schema_cmd.render_find(doc, "c")
    assert f"and 5 more" in out


# --- browse dispatch: the verb routes, and bare --dev still dumps ---------------

@pytest.fixture
def browse_load(monkeypatch):
    calls = []

    def _load(url, max_age=schema.DEFAULT_MAX_AGE_SECONDS, refresh=False):
        calls.append({"url": url, "max_age": max_age, "refresh": refresh})
        return schema.SchemaResult(document=json.dumps(BROWSE_DOC).encode(),
                                   cached=False, elapsed=0.1, cache_written=True)

    monkeypatch.setattr(schema, "load", _load)
    return calls


def test_list_verb_routes_to_the_browser(dev_env, browse_load, capsys):
    schema_cmd.run(["list", "--dev"])
    assert "schemas in shop" in capsys.readouterr().out


def test_show_verb_routes_to_the_browser(dev_env, browse_load, capsys):
    schema_cmd.run(["show", "public.users", "--dev"])
    assert capsys.readouterr().out.startswith("table public.users")


def test_find_verb_routes_to_the_browser(dev_env, browse_load, capsys):
    schema_cmd.run(["find", "email", "--dev"])
    assert "public.users.email" in capsys.readouterr().out


def test_bare_dev_still_dumps_json_not_browse(dev_env, browse_load, capsysbinary):
    schema_cmd.run(["--dev"])
    out = capsysbinary.readouterr()[0]
    assert json.loads(out)["database"] == "shop"   # raw JSON, not the text renderer


def test_browse_flags_reach_the_engine(dev_env, browse_load):
    schema_cmd.run(["list", "--dev", "--refresh"])
    schema_cmd.run(["show", "users", "--dev", "--max-age", "30m"])
    assert browse_load[0]["refresh"] is True
    assert browse_load[1]["max_age"] == 1800
