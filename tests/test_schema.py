"""The `schema` command: introspection, caching, and the CLI surface."""

import pytest

from db_core.core import system, tokens


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
