import json

import pytest

from execute_db.commands import exec as exec_cmd
from execute_db.core.query import QueryResult


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
