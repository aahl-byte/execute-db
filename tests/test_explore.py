"""explore-db: the read-only front-end over the shared engine.

These lock in the two things that make explore-db distinct from execute-db —
the read-only connection option and the separate config directory — plus the
thin front-end wiring.
"""

import pytest

from db_core import app
from db_core.core import query, store, system


# --- read-only enforcement ---------------------------------------------------

class _FakeCursor:
    description = [("n",)]
    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return [(1,)]


class _FakeConn:
    def __init__(self):
        self.committed = False

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def captured_connect(monkeypatch):
    seen = {}

    def fake_connect(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return _FakeConn()

    monkeypatch.setattr(query.psycopg2, "connect", fake_connect)
    return seen


def test_readonly_app_forces_readonly_transaction(captured_connect):
    app.configure(app.AppSpec(name="explore-db", read_only=True, version="t"))
    query.run_query("postgresql://x", "SELECT 1")
    assert captured_connect["kwargs"].get("options") == \
        "-c default_transaction_read_only=on"


def test_readwrite_app_does_not_set_readonly(captured_connect):
    app.configure(app.AppSpec(name="execute-db", read_only=False, version="t"))
    query.run_query("postgresql://x", "SELECT 1")
    assert "options" not in captured_connect["kwargs"]


# --- separate config directory ----------------------------------------------

def test_config_dir_is_per_app(monkeypatch):
    monkeypatch.setattr(store, "_dir_override", None)
    monkeypatch.setattr(system, "in_system_mode", lambda: False)

    app.configure(app.AppSpec(name="explore-db", read_only=True, version="t"))
    assert store.config_dir().name == ".explore-db"

    app.configure(app.AppSpec(name="execute-db", read_only=False, version="t"))
    assert store.config_dir().name == ".execute-db"


# --- front-end wiring --------------------------------------------------------

def test_explore_cli_version(monkeypatch, capsys):
    from explore_db import __version__, cli
    monkeypatch.setenv("EXPLORE_DB_NO_SYSTEM", "1")
    monkeypatch.setattr(cli.sys, "argv", ["explore-db", "--version"])
    cli.main()
    out = capsys.readouterr().out
    assert "explore-db" in out and __version__ in out


def test_explore_help_advertises_read_only(monkeypatch, capsys):
    from explore_db import cli
    monkeypatch.setenv("EXPLORE_DB_NO_SYSTEM", "1")
    monkeypatch.setattr(cli.sys, "argv", ["explore-db", "--help"])
    cli.main()
    out = capsys.readouterr().out
    assert "read-only" in out
    assert "config set" in out and "token create" in out
