"""The `schema` command: introspection, caching, and the CLI surface."""

import re

import pytest

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
