"""The statement lexer: splitting on `;` only where PostgreSQL would.

Pure-function tests; no database, no mocks. Each section pins one lexer rule
from the design doc (docs/plans/2026-07-20-multi-statement-design.md).
"""

from db_core.core.split import split_statements


# --- basic splitting ---------------------------------------------------------

def test_two_statements_split_on_semicolon():
    assert split_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_single_statement_no_semicolon():
    assert split_statements("SELECT 1") == ["SELECT 1"]


def test_trailing_semicolon_creates_no_empty_statement():
    assert split_statements("SELECT 1;") == ["SELECT 1"]


def test_whitespace_only_segments_dropped():
    assert split_statements("SELECT 1; \n\t ; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_empty_input_yields_no_statements():
    assert split_statements("") == []
    assert split_statements("   \n  ") == []


def test_statements_are_stripped_but_internally_intact():
    assert split_statements("\n  UPDATE t\n  SET x = 1\n; ") == ["UPDATE t\n  SET x = 1"]
