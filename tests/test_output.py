import json

import pytest

from db_core.commands import exec as exec_cmd
from db_core.core.query import QueryResult


def rows_result(columns, rows):
    return QueryResult("rows", columns=columns, rows=rows)


# --- table ---------------------------------------------------------------

def test_table_aligns_columns():
    r = rows_result(["id", "name"], [(1, "Alice"), (22, "Bo")])
    out = exec_cmd.format_result(r, "table")
    lines = out.splitlines()
    assert lines[0] == "id | name "
    assert set(lines[1]) <= {"-", "+"}
    assert lines[2] == "1  | Alice"
    assert lines[3] == "22 | Bo   "


def test_table_empty_prints_header_only():
    r = rows_result(["id", "name"], [])
    out = exec_cmd.format_result(r, "table")
    assert out.splitlines()[0] == "id | name"
    assert len(out.splitlines()) == 2  # header + rule


# --- json / jsonl --------------------------------------------------------

def test_json_is_array_of_objects():
    r = rows_result(["id", "name"], [(1, "Alice")])
    assert json.loads(exec_cmd.format_result(r, "json")) == [{"id": 1, "name": "Alice"}]


def test_json_empty_is_empty_array():
    assert exec_cmd.format_result(rows_result(["id"], []), "json") == "[]"


def test_jsonl_one_object_per_line():
    r = rows_result(["id"], [(1,), (2,)])
    out = exec_cmd.format_result(r, "jsonl")
    assert [json.loads(l) for l in out.splitlines()] == [{"id": 1}, {"id": 2}]


def test_jsonl_empty_is_empty_string():
    assert exec_cmd.format_result(rows_result(["id"], []), "jsonl") == ""


# --- csv -----------------------------------------------------------------

def test_csv_has_header_and_rows():
    r = rows_result(["id", "name"], [(1, "Alice"), (2, "B,ob")])
    out = exec_cmd.format_result(r, "csv")
    assert out.splitlines()[0] == "id,name"
    assert out.splitlines()[1] == "1,Alice"
    assert out.splitlines()[2] == '2,"B,ob"'  # quoting for embedded comma


def test_csv_empty_is_header_only():
    assert exec_cmd.format_result(rows_result(["id", "name"], []), "csv") == "id,name"


# --- list ----------------------------------------------------------------

def test_list_single_column_is_bare_values():
    r = rows_result(["name"], [("Alice",), ("Bob",)])
    assert exec_cmd.format_result(r, "list") == "Alice\nBob"


def test_list_multi_column_is_tab_joined():
    r = rows_result(["id", "name"], [(1, "Alice")])
    assert exec_cmd.format_result(r, "list") == "1\tAlice"


# --- edge cases: NULL and non-scalar cells -------------------------------

def test_null_renders_as_literal_in_text_formats():
    r = rows_result(["id", "name"], [(1, None)])
    assert "NULL" in exec_cmd.format_result(r, "table")
    assert exec_cmd.format_result(r, "csv").splitlines()[1] == "1,NULL"
    assert exec_cmd.format_result(r, "list") == "1\tNULL"


def test_null_is_json_null_in_json_formats():
    r = rows_result(["name"], [(None,)])
    assert json.loads(exec_cmd.format_result(r, "json")) == [{"name": None}]


def test_jsonb_cell_is_json_encoded_in_text_formats():
    r = rows_result(["data"], [({"a": 1},)])
    assert exec_cmd.format_result(r, "list") == '{"a": 1}'
    assert exec_cmd.format_result(r, "csv").splitlines()[1] == '"{""a"": 1}"'


def test_non_row_kinds_produce_no_stdout():
    assert exec_cmd.format_result(QueryResult("count", rowcount=3), "table") == ""
    assert exec_cmd.format_result(QueryResult("ok"), "json") == ""


# --- vertical / expanded -------------------------------------------------

def test_vertical_blocks_one_per_row():
    r = rows_result(["id", "name"], [(1, "Alice"), (2, "Bob")])
    out = exec_cmd.format_result(r, "vertical")
    blocks = out.split("\n\n")
    assert blocks[0].splitlines() == ["[ row 1 ]", "id   | 1", "name | Alice"]
    assert blocks[1].splitlines() == ["[ row 2 ]", "id   | 2", "name | Bob"]


def test_vertical_renders_null_and_jsonb():
    r = rows_result(["a", "data"], [(None, {"k": 1})])
    out = exec_cmd.format_result(r, "vertical")
    assert "a    | NULL" in out
    assert 'data | {"k": 1}' in out


def test_vertical_empty_is_empty_string():
    assert exec_cmd.format_result(rows_result(["id"], []), "vertical") == ""


# --- pager routing -------------------------------------------------------

def test_emit_prints_plainly_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(exec_cmd.sys.stdout, "isatty", lambda: False)
    called = []
    monkeypatch.setattr(exec_cmd, "_run_pager", lambda t: called.append(t) or True)
    exec_cmd._emit("hello", use_pager=True)
    assert called == []                       # pager not used off a TTY
    assert capsys.readouterr().out == "hello\n"


def test_emit_uses_pager_at_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(exec_cmd.sys.stdout, "isatty", lambda: True)
    called = []
    monkeypatch.setattr(exec_cmd, "_run_pager", lambda t: called.append(t) or True)
    exec_cmd._emit("hello", use_pager=True)
    assert called == ["hello"]                # paged, not printed
    assert capsys.readouterr().out == ""


def test_emit_falls_back_to_print_when_pager_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(exec_cmd.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(exec_cmd, "_run_pager", lambda t: False)  # no pager
    exec_cmd._emit("hello", use_pager=True)
    assert capsys.readouterr().out == "hello\n"


def test_emit_no_pager_flag_forces_plain(monkeypatch, capsys):
    monkeypatch.setattr(exec_cmd.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(exec_cmd, "_run_pager",
                        lambda t: (_ for _ in ()).throw(AssertionError("paged")))
    exec_cmd._emit("hello", use_pager=False)  # --no-pager path
    assert capsys.readouterr().out == "hello\n"


def test_broken_pager_env_falls_back_without_losing_output(monkeypatch, capsys):
    # A stale/misspelled $PAGER must not swallow the result data.
    monkeypatch.setenv("PAGER", "this-pager-does-not-exist-xyz")
    assert exec_cmd._run_pager("important data") is False
    monkeypatch.setattr(exec_cmd.sys.stdout, "isatty", lambda: True)
    exec_cmd._emit("important data", use_pager=True)
    assert capsys.readouterr().out == "important data\n"


def test_empty_pager_env_falls_back(monkeypatch):
    monkeypatch.setenv("PAGER", "   ")
    assert exec_cmd._run_pager("x") is False


def test_machine_formats_are_never_paged():
    assert "json" not in exec_cmd.HUMAN_FORMATS
    assert "csv" not in exec_cmd.HUMAN_FORMATS
    assert set(exec_cmd.HUMAN_FORMATS) == {"table", "vertical"}
