"""--multi: splitting a script and running each statement in one transaction.

Core layer (`query.run_multi`) here; the command-layer rendering and flag
wiring are in test_multi_output.py. The fakes script per-statement cursor
behavior; no real database.
"""

import pytest

from db_core import app
from db_core.core import query

from .conftest import ConnError, ServerError


class _ScriptedCursor:
    """A cursor whose description/rowcount are scripted per executed statement.

    `script` maps a statement (exact text) to one of:
      ("rows", columns, rows) | ("count", n) | ("raise", exc)
    Statements not in the script behave as DDL ("ok").
    """

    def __init__(self, script):
        self.script = script
        self.executed = []
        self.description = None
        self.rowcount = -1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        entry = self.script.get(sql, ("ok",))
        if entry[0] == "raise":
            raise entry[1]
        if entry[0] == "rows":
            self.description = [(c,) for c in entry[1]]
            self._rows = entry[2]
            self.rowcount = len(entry[2])
        elif entry[0] == "count":
            self.description = None
            self.rowcount = entry[1]
        else:
            self.description = None
            self.rowcount = -1

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


@pytest.fixture
def connect(monkeypatch):
    """Install a scripted connection; returns a dict to configure + inspect."""
    state = {"script": {}, "conn": None, "kwargs": None}

    def fake_connect(url, **kwargs):
        state["kwargs"] = kwargs
        state["conn"] = _Conn(_ScriptedCursor(state["script"]))
        return state["conn"]

    monkeypatch.setattr(query.psycopg2, "connect", fake_connect)
    return state


MIGRATION = "UPDATE t SET x = 1; SELECT id FROM t; CREATE INDEX i ON t(x)"


def test_each_statement_executes_in_order_on_one_cursor(connect):
    results = query.run_multi("postgresql://x", MIGRATION)
    assert connect["conn"]._cursor.executed == [
        "UPDATE t SET x = 1", "SELECT id FROM t", "CREATE INDEX i ON t(x)"]
    assert [r.index for r in results] == [1, 2, 3]


def test_per_statement_classification(connect):
    connect["script"]["UPDATE t SET x = 1"] = ("count", 12)
    connect["script"]["SELECT id FROM t"] = ("rows", ["id"], [(1,), (2,)])
    results = query.run_multi("postgresql://x", MIGRATION)
    assert [r.result.kind for r in results] == ["count", "rows", "ok"]
    assert results[0].result.rowcount == 12
    assert results[1].result.columns == ["id"]
    assert results[1].result.rows == [(1,), (2,)]


def test_one_commit_at_the_end(connect):
    query.run_multi("postgresql://x", MIGRATION)
    assert connect["conn"].committed == 1
    assert connect["conn"].rolled_back == 0
    assert connect["conn"].closed


def test_failure_rolls_back_everything_and_names_the_statement(connect):
    boom = RuntimeError("column does not exist")
    connect["script"]["SELECT id FROM t"] = ("raise", boom)
    with pytest.raises(query.StatementError) as excinfo:
        query.run_multi("postgresql://x", MIGRATION)
    assert str(excinfo.value) == "statement 2 of 3 failed"
    assert excinfo.value.index == 2 and excinfo.value.total == 3
    # The driver error is chained for the disclosure gate to inspect.
    assert excinfo.value.__cause__ is boom
    assert connect["conn"].committed == 0
    assert connect["conn"].rolled_back == 1
    assert connect["conn"].closed
    # Nothing after the failing statement was sent to the server.
    assert connect["conn"]._cursor.executed == [
        "UPDATE t SET x = 1", "SELECT id FROM t"]


def test_previews_are_first_line_truncated(connect):
    long = "SELECT " + "x" * 200 + "\nFROM t"
    results = query.run_multi("postgresql://x", f"{long}; SELECT 1")
    assert results[0].preview == ("SELECT " + "x" * 200)[:77] + "..."
    assert "\n" not in results[0].preview
    assert results[1].preview == "SELECT 1"


def test_read_only_app_still_forces_readonly_transaction(connect):
    app.configure(app.AppSpec(name="explore-db", read_only=True, version="t"))
    query.run_multi("postgresql://x", "SELECT 1; SELECT 2")
    assert connect["kwargs"].get("options") == "-c default_transaction_read_only=on"


def test_server_error_reads_through_a_statement_error(connect):
    """The disclosure gate must find the server's words under StatementError."""
    exc = ServerError("42703", 'column "nope" does not exist')
    connect["script"]["SELECT id FROM t"] = ("raise", exc)
    with pytest.raises(query.StatementError) as excinfo:
        query.run_multi("postgresql://x", MIGRATION)
    assert query.server_error(excinfo.value) == 'column "nope" does not exist'


def test_connection_error_stays_opaque_through_a_statement_error(connect):
    """The other half of the split: no SQLSTATE, no disclosure — even chained."""
    exc = ConnError('could not translate host name "db-internal" to address')
    connect["script"]["SELECT id FROM t"] = ("raise", exc)
    with pytest.raises(query.StatementError) as excinfo:
        query.run_multi("postgresql://x", MIGRATION)
    assert query.server_error(excinfo.value) is None
