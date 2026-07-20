"""--multi at the command layer: flag wiring, renderers, the no-flag hint.

`run()` is exercised end-to-end with the env/URL/query layers monkeypatched
out, so these tests own exactly one thing: what lands on stdout vs stderr.
"""

import json

import pytest

from db_core.commands import exec as exec_cmd
from db_core.core import query
from db_core.core.query import QueryResult, StatementResult

from .conftest import ConnError, ServerError


@pytest.fixture
def wired(monkeypatch):
    """Bypass env discovery and URL resolution; run() goes straight to query."""
    monkeypatch.setattr(exec_cmd, "discover_envs", lambda: ["dev"])
    monkeypatch.setattr(exec_cmd.store, "load_database_url",
                        lambda env: "postgresql://x")
    monkeypatch.setattr(exec_cmd, "in_system_mode", lambda: False)


def _results():
    return [
        StatementResult(1, "UPDATE t SET x = 1", QueryResult("count", rowcount=12)),
        StatementResult(2, "SELECT id, name FROM t",
                        QueryResult("rows", columns=["id", "name"],
                                    rows=[(1, "Alice")])),
        StatementResult(3, "CREATE INDEX i ON t(x)", QueryResult("ok")),
    ]


# --- machine formats: the envelope ------------------------------------------

def test_json_envelope_has_one_object_per_statement(wired, monkeypatch, capsys):
    monkeypatch.setattr(query, "run_multi", lambda url, sql: _results())
    exec_cmd.run(["--dev", "--multi", "-o", "json", "ignored"])
    out = json.loads(capsys.readouterr().out)
    assert out == [
        {"statement": 1, "preview": "UPDATE t SET x = 1",
         "kind": "count", "rowcount": 12},
        {"statement": 2, "preview": "SELECT id, name FROM t", "kind": "rows",
         "columns": ["id", "name"], "rows": [{"id": 1, "name": "Alice"}]},
        {"statement": 3, "preview": "CREATE INDEX i ON t(x)", "kind": "ok"},
    ]


def test_json_shape_follows_the_flag_not_the_count(wired, monkeypatch, capsys):
    # One statement under --multi is still a one-element ARRAY: a consumer
    # must get a deterministic shape.
    monkeypatch.setattr(query, "run_multi", lambda url, sql: [_results()[1]])
    exec_cmd.run(["--dev", "--multi", "-o", "json", "ignored"])
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list) and len(out) == 1


def test_jsonl_one_object_per_line(wired, monkeypatch, capsys):
    monkeypatch.setattr(query, "run_multi", lambda url, sql: _results())
    exec_cmd.run(["--dev", "--multi", "-o", "jsonl", "ignored"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["kind"] == "count"
    assert json.loads(lines[1])["rows"] == [{"id": 1, "name": "Alice"}]


# --- human formats -----------------------------------------------------------

def test_table_shows_headers_for_row_statements_and_stderr_for_the_rest(
        wired, monkeypatch, capsys):
    monkeypatch.setattr(query, "run_multi", lambda url, sql: _results())
    exec_cmd.run(["--dev", "--multi", "--no-pager", "ignored"])
    captured = capsys.readouterr()
    assert "-- statement 2 --" in captured.out
    assert "Alice" in captured.out
    # Non-row statements are status, not data: stderr, numbered.
    assert "[1] Rows affected: 12" in captured.err
    assert "[3] Statement executed." in captured.err
    assert "Rows affected" not in captured.out


def test_meta_lines_are_numbered_per_statement(wired, monkeypatch, capsys):
    monkeypatch.setattr(query, "run_multi", lambda url, sql: _results())
    exec_cmd.run(["--dev", "--multi", "--no-pager", "--meta", "ignored"])
    assert "[2] 1 row, columns: id, name" in capsys.readouterr().err


# --- csv/list rejection ------------------------------------------------------

@pytest.mark.parametrize("fmt", ["csv", "list"])
def test_multi_rejects_unrenderable_formats(wired, fmt, capsys):
    with pytest.raises(SystemExit) as excinfo:
        exec_cmd.run(["--dev", "--multi", "-o", fmt, "SELECT 1"])
    assert excinfo.value.code == 2  # argparse usage error
    err = capsys.readouterr().err
    assert "json" in err and "jsonl" in err


# --- the no-flag hint --------------------------------------------------------

def test_multi_statement_script_without_flag_hints_at_multi(
        wired, monkeypatch, capsys):
    monkeypatch.setattr(query, "run_query", lambda url, sql: QueryResult("ok"))
    exec_cmd.run(["--dev", "UPDATE a SET x = 1; UPDATE b SET y = 2"])
    err = capsys.readouterr().err
    assert "2 statements" in err and "--multi" in err


def test_single_statement_gets_no_hint(wired, monkeypatch, capsys):
    monkeypatch.setattr(query, "run_query", lambda url, sql: QueryResult("ok"))
    exec_cmd.run(["--dev", "SELECT 1"])
    assert "--multi" not in capsys.readouterr().err


def test_without_flag_the_sql_reaches_execute_unsplit(wired, monkeypatch, capsys):
    """Byte-identical default path: the original string, not a re-join."""
    seen = {}

    def fake_run_query(url, sql):
        seen["sql"] = sql
        return QueryResult("ok")

    monkeypatch.setattr(query, "run_query", fake_run_query)
    sql = "SELECT 1;  SELECT 2;"
    exec_cmd.run(["--dev", sql])
    assert seen["sql"] == sql


# --- failure reporting -------------------------------------------------------

def _raising(exc, index, total):
    """A run_multi stand-in that fails the way the real one does: the driver
    error in flight (so it lands in __context__/__cause__), StatementError out."""
    def boom(url, sql):
        try:
            raise exc
        except type(exc) as e:
            raise query.StatementError(index, total) from e
    return boom


def test_statement_failure_names_position_and_cause(wired, monkeypatch, capsys):
    exc = ServerError("42703", 'column "nope" does not exist')
    monkeypatch.setattr(query, "run_multi", _raising(exc, 2, 3))
    with pytest.raises(SystemExit):
        exec_cmd.run(["--dev", "--multi", "ignored"])
    err = capsys.readouterr().err
    assert "statement 2 of 3 failed" in err
    assert 'column "nope" does not exist' in err


def test_statement_failure_in_system_mode_discloses_only_the_server_words(
        wired, monkeypatch, capsys):
    exc = ConnError('could not translate host name "db-internal" to address')
    monkeypatch.setattr(query, "run_multi", _raising(exc, 1, 2))
    monkeypatch.setattr(exec_cmd, "in_system_mode", lambda: True)
    with pytest.raises(SystemExit):
        exec_cmd.run(["--dev", "--multi", "ignored"])
    err = capsys.readouterr().err
    assert "statement 1 of 2 failed" in err   # position: caller's own input
    assert "db-internal" not in err           # connection text: withheld
