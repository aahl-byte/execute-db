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


# --- quoting -----------------------------------------------------------------
# The four rules that make naive ;-splitting wrong. See the design doc.

def test_semicolon_inside_string_literal_is_not_a_boundary():
    assert split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1") == \
        ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]


def test_doubled_quote_is_an_escape_not_a_close():
    assert split_statements("SELECT 'it''s; fine'; SELECT 2") == \
        ["SELECT 'it''s; fine'", "SELECT 2"]


def test_backslash_is_literal_in_plain_strings():
    # 'a\' is a COMPLETE string containing one backslash
    # (standard_conforming_strings=on); treating \ as an escape here would
    # swallow the closing quote and eat the ; after it.
    assert split_statements(r"SELECT 'a\'; SELECT 2") == \
        [r"SELECT 'a\'", "SELECT 2"]


def test_backslash_is_an_escape_in_e_strings():
    # E'a\'; b' is one string (backslash escapes the quote); a lexer that
    # treats it as plain would close at the \' and split on the ; inside.
    assert split_statements(r"SELECT E'a\'; b'; SELECT 2") == \
        [r"SELECT E'a\'; b'", "SELECT 2"]


def test_identifier_ending_in_e_is_not_an_e_string():
    # `namE'x\'` — the E belongs to the identifier, the string is plain, so
    # the backslash is literal and the string closes before the semicolon.
    assert split_statements(r"SELECT namE'x\'; SELECT 2") == \
        [r"SELECT namE'x\'", "SELECT 2"]


def test_semicolon_inside_double_quoted_identifier():
    assert split_statements('SELECT "a;b" FROM t; SELECT 2') == \
        ['SELECT "a;b" FROM t', "SELECT 2"]


def test_dollar_quoted_body_is_opaque():
    fn = ("CREATE FUNCTION f() RETURNS void AS $$\n"
          "BEGIN\n  UPDATE t SET x = 1;\n  DELETE FROM u;\nEND;\n"
          "$$ LANGUAGE plpgsql")
    assert split_statements(fn + "; SELECT 1") == [fn, "SELECT 1"]


def test_tagged_dollar_quote_closes_only_on_its_own_tag():
    # $body$ ... $$ ... $body$ — the inner $$ is body text, not a delimiter.
    fn = "SELECT $body$ text with $$ and ; inside $body$"
    assert split_statements(fn + "; SELECT 2") == [fn, "SELECT 2"]


def test_positional_param_does_not_open_a_dollar_quote():
    assert split_statements("EXECUTE p($1); SELECT 2") == \
        ["EXECUTE p($1)", "SELECT 2"]


def test_unterminated_string_swallows_the_rest():
    # Garbage in, one statement out — the server reports the real error.
    assert split_statements("SELECT 'oops; SELECT 2") == ["SELECT 'oops; SELECT 2"]


# --- comments ----------------------------------------------------------------

def test_semicolon_in_line_comment_is_not_a_boundary():
    sql = "SELECT 1 -- trailing; note\n; SELECT 2"
    assert split_statements(sql) == ["SELECT 1 -- trailing; note", "SELECT 2"]


def test_comment_marker_inside_a_string_is_data():
    assert split_statements("SELECT '-- not a comment'; SELECT 2") == \
        ["SELECT '-- not a comment'", "SELECT 2"]


def test_block_comments_nest():
    sql = "SELECT 1 /* outer /* inner; */ still; out */; SELECT 2"
    assert split_statements(sql) == \
        ["SELECT 1 /* outer /* inner; */ still; out */", "SELECT 2"]


def test_comment_only_segment_is_dropped():
    assert split_statements("SELECT 1;\n-- done\n") == ["SELECT 1"]
    assert split_statements("/* header */; SELECT 1") == ["SELECT 1"]


def test_leading_comment_stays_attached_to_its_statement():
    sql = "-- migrate users\nUPDATE users SET x = 1"
    assert split_statements(sql) == [sql]
