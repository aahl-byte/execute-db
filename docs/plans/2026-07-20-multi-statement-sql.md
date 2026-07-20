# Multi-statement SQL (`--multi`) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `execute-db --multi` (and `explore-db --multi`) splits a SQL script into statements, runs them all in one transaction, and shows every statement's result — including a machine-parseable `-o json`/`-o jsonl` envelope. Default (no flag) behavior stays byte-identical, plus a stderr hint when a multi-statement script is run without the flag.

**Design:** `docs/plans/2026-07-20-multi-statement-design.md` — read it first; it records every decision and rejection.

**Architecture:** A new pure lexer `db_core/core/split.py` (single-pass scanner, zero SQL keywords) splits scripts. `db_core/core/query.py` gains `run_multi()` executing each statement on one cursor in one transaction. `db_core/commands/exec.py` gains the `--multi` flag, the multi-result renderers, and the no-flag hint line. Both binaries share all of it.

**Tech stack:** Python (stdlib + psycopg2), pytest. No new dependencies.

**Conventions that bite:**
- Tests never touch a real database: `psycopg2.connect` is monkeypatched (see `tests/test_explore.py:_FakeCursor`), and command-layer tests monkeypatch module globals on `exec_cmd` directly.
- `conftest.py` auto-configures the app as `execute-db` (read/write); reconfigure via `app.configure(...)` inside a test when you need the read-only spec.
- stdout carries result data ONLY; all status goes to stderr. Every output assertion distinguishes `capsys.readouterr().out` from `.err`.
- Run tests with `python -m pytest` from the repo root.
- Commit after every green step; messages end with the Co-Authored-By line per global instructions.

---

### Task 1: Lexer — basic statement splitting

**Files:**
- Create: `db_core/core/split.py`
- Create: `tests/test_split.py`

**Step 1: Write the failing tests**

```python
# tests/test_split.py
"""The statement lexer: splitting on `;` only where PostgreSQL would.

Pure-function tests; no database, no mocks. Each class pins one lexer rule
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
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_split.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db_core.core.split'`

**Step 3: Write the minimal implementation**

```python
# db_core/core/split.py
"""Split a SQL script into its top-level statements.

A single-pass character scanner that knows PostgreSQL's quoting and comment
forms but ZERO SQL keywords: `;` is a statement boundary only in plain text,
and everything inside '...' / E'...' / "..." / $tag$...$tag$ / -- / /* */ is
opaque. Embedded BEGIN/COMMIT are not special — the server enforces their
semantics (see the design doc's rejected-alternatives list).

Unterminated constructs (an unclosed quote or comment) swallow the rest of the
input rather than raising: the text still gets executed, and the *server* is
the authority on whether it is valid SQL.
"""

import re

# A dollar-quote opener: $$ or $tag$ (tag = identifier, no leading digit —
# which is what keeps positional params like $1 from opening a quote).
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")


def split_statements(sql: str) -> list:
    """The non-empty statements of `sql`, in order, outer whitespace stripped.

    Segments that are empty or contain only comments are dropped: they are
    artifacts of splitting (trailing `;`, a file-footer comment), and executing
    an empty string is a server error.
    """
    boundaries = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == ";":
            boundaries.append(i)
            i += 1
        elif c == "'":
            i = _skip_quoted(sql, i, "'", backslash=_is_estring(sql, i))
        elif c == '"':
            i = _skip_quoted(sql, i, '"', backslash=False)
        elif c == "$":
            m = _DOLLAR.match(sql, i)
            if m:
                end = sql.find(m.group(0), m.end())
                i = n if end == -1 else end + len(m.group(0))
            else:
                i += 1
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
        elif c == "/" and sql.startswith("/*", i):
            i = _skip_block_comment(sql, i)
        else:
            i += 1

    statements = []
    start = 0
    for b in boundaries + [n]:
        stmt = sql[start:b].strip()
        if stmt and _has_content(stmt):
            statements.append(stmt)
        start = b + 1
    return statements


def _is_estring(sql: str, i: int) -> bool:
    """Is the quote at `i` the body of an E'...' string?

    The E must itself start a token: in `namE'x'` the E belongs to the
    identifier `namE`, and the string is a plain one (backslash literal).
    """
    if i == 0 or sql[i - 1] not in "eE":
        return False
    return i == 1 or not (sql[i - 2].isalnum() or sql[i - 2] == "_")


def _skip_quoted(sql: str, i: int, quote: str, backslash: bool) -> int:
    """From the opening quote at `i`, the index just past the closing quote.

    A doubled quote ('' / "") is an escape in both kinds; backslash is an
    escape ONLY in E'...' strings (standard_conforming_strings is on by
    default, so in a plain string a backslash is a literal character).
    """
    i += 1
    n = len(sql)
    while i < n:
        c = sql[i]
        if backslash and c == "\\":
            i += 2
        elif c == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
            else:
                return i + 1
        else:
            i += 1
    return n


def _skip_block_comment(sql: str, i: int) -> int:
    """From the `/*` at `i`, the index just past its close. PG comments NEST."""
    depth = 0
    n = len(sql)
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def _has_content(stmt: str) -> bool:
    """True if anything in `stmt` sits outside a comment (so it is executable)."""
    i, n = 0, len(stmt)
    while i < n:
        if stmt.startswith("--", i):
            j = stmt.find("\n", i)
            i = n if j == -1 else j + 1
        elif stmt.startswith("/*", i):
            i = _skip_block_comment(stmt, i)
        elif stmt[i].isspace():
            i += 1
        else:
            return True
    return False
```

**Step 4: Run to verify pass**

Run: `python -m pytest tests/test_split.py -v`
Expected: 6 PASS

**Step 5: Commit**

```bash
git add db_core/core/split.py tests/test_split.py
git commit -m "feat: statement lexer — basic splitting on top-level semicolons

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Lexer — quoting rules

The implementation from Task 1 already contains the quoting machinery; this task pins it with tests so a future "simplification" cannot silently break it. If any test fails, fix `split.py`, do not adjust the test.

**Files:**
- Modify: `tests/test_split.py` (append)
- Possibly fix: `db_core/core/split.py`

**Step 1: Write the tests**

```python
# append to tests/test_split.py

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
    # E'a\'' is one string (backslash escapes the quote); a lexer that treats
    # it as plain would close at the \' and split on the ; inside.
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
```

**Step 2: Run**

Run: `python -m pytest tests/test_split.py -v`
Expected: all PASS (the Task 1 implementation covers these). Any failure is an implementation bug — fix `split.py`.

**Step 3: Commit**

```bash
git add tests/test_split.py
git commit -m "test: pin lexer quoting rules (strings, E-strings, dollar quoting)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Lexer — comment rules

**Files:**
- Modify: `tests/test_split.py` (append)
- Possibly fix: `db_core/core/split.py`

**Step 1: Write the tests**

```python
# append to tests/test_split.py

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
```

**Step 2: Run**

Run: `python -m pytest tests/test_split.py -v`
Expected: all PASS. Fix `split.py` if not.

**Step 3: Commit**

```bash
git add tests/test_split.py
git commit -m "test: pin lexer comment rules (line, nested block, comment-only)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Refactor `query.py` — extract `_connect` and `_classify`

Behavior-preserving refactor so `run_multi` (Task 5) can share the connection
setup and the per-statement classification instead of duplicating them. No new
tests; the existing suite is the check.

**Files:**
- Modify: `db_core/core/query.py:26-54`

**Step 1: Refactor**

Replace `run_query` (keep its docstring-comment about read-only apps on `_connect`):

```python
def _connect(database_url: str):
    # In read-only apps (explore-db) the server itself rejects any write: a
    # read-only transaction fails on INSERT/UPDATE/DELETE/DDL, so the guarantee
    # does not depend on parsing the SQL. Committing a read-only transaction is
    # harmless, so the surrounding flow stays identical for both apps.
    connect_kwargs = {"sslmode": "require"}
    if app.current().read_only:
        connect_kwargs["options"] = "-c default_transaction_read_only=on"
    return psycopg2.connect(database_url, **connect_kwargs)


def _classify(cur) -> QueryResult:
    """What one just-executed statement produced, read off the cursor."""
    if cur.description is not None:
        columns = [desc[0] for desc in cur.description]
        return QueryResult("rows", columns=columns, rows=cur.fetchall())
    if cur.rowcount >= 0:
        return QueryResult("count", rowcount=cur.rowcount)
    # rowcount is -1 when undefined (e.g. DDL such as CREATE/ALTER).
    return QueryResult("ok")


def run_query(database_url: str, sql: str) -> QueryResult:
    conn = _connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            result = _classify(cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**Step 2: Run the whole suite**

Run: `python -m pytest`
Expected: all PASS (pure refactor).

**Step 3: Commit**

```bash
git add db_core/core/query.py
git commit -m "refactor: extract _connect/_classify from run_query for reuse

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `run_multi` — per-statement execution in one transaction

**Files:**
- Modify: `db_core/core/query.py`
- Create: `tests/test_multi.py`

**Step 1: Write the failing tests**

```python
# tests/test_multi.py
"""--multi: splitting a script and running each statement in one transaction.

Core layer (`query.run_multi`) here; the command-layer rendering and flag
wiring are in test_multi_output.py. The fakes script per-statement cursor
behavior; no real database.
"""

import pytest

from db_core import app
from db_core.core import query


class _ScriptedCursor:
    """A cursor whose description/rowcount are scripted per executed statement.

    `script` maps a statement (exact text) to one of:
      ("rows", columns, rows) | ("count", n) | ("ok",) | ("raise", exc)
    Statements not in the script behave as "ok".
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
    from tests.conftest import ServerError
    exc = ServerError("42703", 'column "nope" does not exist')
    connect["script"]["SELECT id FROM t"] = ("raise", exc)
    with pytest.raises(query.StatementError) as excinfo:
        query.run_multi("postgresql://x", MIGRATION)
    assert query.server_error(excinfo.value) == 'column "nope" does not exist'
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_multi.py -v`
Expected: FAIL — `AttributeError: module 'db_core.core.query' has no attribute 'run_multi'`

**Step 3: Implement**

In `db_core/core/query.py`: add to the imports `from .split import split_statements`, and add after the `QueryResult` dataclass:

```python
@dataclass
class StatementResult:
    """One statement's outcome under --multi.

    `preview` is the statement's own first line (truncated) — the caller wrote
    it, so echoing it back discloses nothing.
    """
    index: int          # 1-based position among the executed statements
    preview: str
    result: QueryResult


class StatementError(Exception):
    """Statement `index` of `total` failed; the driver's error is __cause__.

    index/total derive only from the caller's own input, so str(self) is safe
    to disclose even in system mode. The cause is NOT baked into the message:
    the command layer routes it through `server_error`, same as ever.
    """

    def __init__(self, index: int, total: int):
        super().__init__(f"statement {index} of {total} failed")
        self.index = index
        self.total = total
```

And after `run_query`:

```python
def _preview(stmt: str, limit: int = 80) -> str:
    line = stmt.splitlines()[0] if stmt else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


def run_multi(database_url: str, sql: str) -> "list[StatementResult]":
    """Split `sql` and run each statement on one cursor in one transaction.

    Same atomicity as run_query — commit once at the end, roll everything back
    on any failure. Embedded BEGIN/COMMIT are executed as-is: the server
    enforces their semantics, exactly as it does under single-execute.
    """
    statements = split_statements(sql)
    conn = _connect(database_url)
    try:
        results = []
        with conn.cursor() as cur:
            for index, stmt in enumerate(statements, 1):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    raise StatementError(index, len(statements)) from e
                results.append(
                    StatementResult(index, _preview(stmt), _classify(cur)))
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**Step 4: Run to verify pass**

Run: `python -m pytest tests/test_multi.py -v`
Expected: 8 PASS. Then `python -m pytest` — everything else still green.

**Step 5: Commit**

```bash
git add db_core/core/query.py tests/test_multi.py
git commit -m "feat: run_multi — per-statement execution in one transaction

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Command layer — `--multi` flag, renderers, csv/list rejection

**Files:**
- Modify: `db_core/commands/exec.py`
- Create: `tests/test_multi_output.py`

**Step 1: Write the failing tests**

```python
# tests/test_multi_output.py
"""--multi at the command layer: flag wiring, renderers, the no-flag hint.

`run()` is exercised end-to-end with the env/URL/query layers monkeypatched
out, so these tests own exactly one thing: what lands on stdout vs stderr.
"""

import json

import pytest

from db_core.commands import exec as exec_cmd
from db_core.core import query
from db_core.core.query import QueryResult, StatementResult


@pytest.fixture
def wired(monkeypatch):
    """Bypass env discovery and URL resolution; capture what run() executes."""
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

def test_statement_failure_names_position_and_cause(wired, monkeypatch, capsys):
    from tests.conftest import ServerError

    def boom(url, sql):
        try:
            raise ServerError("42703", 'column "nope" does not exist')
        except ServerError:
            raise query.StatementError(2, 3) from None

    monkeypatch.setattr(query, "run_multi", boom)
    with pytest.raises(SystemExit):
        exec_cmd.run(["--dev", "--multi", "ignored"])
    err = capsys.readouterr().err
    assert "statement 2 of 3 failed" in err
    assert 'column "nope" does not exist' in err


def test_statement_failure_in_system_mode_discloses_only_the_server_words(
        wired, monkeypatch, capsys):
    from tests.conftest import ConnError

    def boom(url, sql):
        try:
            raise ConnError('could not translate host name "db-internal"')
        except ConnError:
            raise query.StatementError(1, 2) from None

    monkeypatch.setattr(query, "run_multi", boom)
    monkeypatch.setattr(exec_cmd, "in_system_mode", lambda: True)
    with pytest.raises(SystemExit):
        exec_cmd.run(["--dev", "--multi", "ignored"])
    err = capsys.readouterr().err
    assert "statement 1 of 2 failed" in err   # position: caller's own input
    assert "db-internal" not in err           # connection text: withheld
```

Note on the two failure tests: `raise ... from None` sets `__cause__ = None`
but `__context__` is still the in-flight exception — which is exactly the
chain `server_error` walks and the non-system path must NOT rely on. If the
implementation only reads `__cause__`, the first failure test will catch it;
adjust the implementation (below) which reads `__context__` via
`server_error` for disclosure and falls back to `__context__` for the
verbose path too.

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_multi_output.py -v`
Expected: FAIL — `unrecognized arguments: --multi` (and import error for `StatementResult` until Task 5 is merged — it is).

**Step 3: Implement in `db_core/commands/exec.py`**

3a. Import the lexer — extend the existing core imports:

```python
from ..core.split import split_statements
```

3b. In `build_parser`, after the `--no-pager` argument (`exec.py:69-71`):

```python
    parser.add_argument("--multi", action="store_true",
                        help="split the SQL into its statements and show every "
                             "statement's result (same single transaction; "
                             "-o csv/list are not supported — use json or jsonl)")
```

3c. Add the renderers after `_print_result` (`exec.py:201`):

```python
def _statement_obj(r: query.StatementResult) -> dict:
    """One statement as a JSON-able object for the --multi envelope."""
    obj = {"statement": r.index, "preview": r.preview, "kind": r.result.kind}
    if r.result.kind == "rows":
        obj["columns"] = r.result.columns
        obj["rows"] = [dict(zip(r.result.columns, row)) for row in r.result.rows]
    elif r.result.kind == "count":
        obj["rowcount"] = r.result.rowcount
    return obj


def _print_multi(results: list, fmt: str = "table",
                 meta: bool = False, pager: bool = True):
    """Render every statement's result (--multi).

    json/jsonl put EVERYTHING (including count/ok statements) on stdout so a
    machine consumer never has to parse stderr; the shape follows the flag,
    not the statement count. Human formats keep the stdout=data/stderr=status
    split: row sets as headed blocks, everything else as numbered stderr lines.
    """
    if fmt == "json":
        print(json.dumps([_statement_obj(r) for r in results],
                         indent=2, default=str))
        return
    if fmt == "jsonl":
        for r in results:
            print(json.dumps(_statement_obj(r), default=str))
        return

    blocks = []
    for r in results:
        q = r.result
        if q.kind == "rows":
            data = format_result(q, fmt)
            block = f"-- statement {r.index} --"
            if data:
                block += "\n" + data
            blocks.append(block)
            if meta:
                n = len(q.rows)
                print(f"[{r.index}] {n} row{'' if n == 1 else 's'}, "
                      f"columns: {', '.join(q.columns)}", file=sys.stderr)
        elif q.kind == "count":
            print(f"[{r.index}] Rows affected: {q.rowcount}", file=sys.stderr)
        else:
            print(f"[{r.index}] Statement executed.", file=sys.stderr)
    if blocks:
        _emit("\n\n".join(blocks), use_pager=pager and fmt in HUMAN_FORMATS)
```

3d. In `run()`, right after `args = parser.parse_args(argv)` (`exec.py:210`):

```python
    if args.multi and args.format in ("csv", "list"):
        parser.error(f"--multi cannot render multiple result sets as "
                     f"{args.format}; use -o json or -o jsonl (or drop --multi "
                     "for the last statement's result only)")
```

3e. Replace the try block at `exec.py:239-253`:

```python
    # Counting is not splitting: without --multi the ORIGINAL string still goes
    # to a single execute(), so the lexer can hint but never corrupt.
    n_statements = len(split_statements(sql))

    try:
        if args.multi:
            _print_multi(query.run_multi(database_url, sql),
                         args.format, args.meta, args.pager)
        else:
            _print_result(query.run_query(database_url, sql),
                          args.format, args.meta, args.pager)
            if n_statements > 1:
                print(f"note: {n_statements} statements ran in one transaction; "
                      "only the last result is shown.\n"
                      "      Re-run with --multi to see each statement's result.",
                      file=sys.stderr)
    except Exception as e:
        # Over sudo the caller may be an agent, and a psycopg2 CONNECTION error
        # can echo host/user/dbname — so that detail stays withheld. But a
        # SERVER-side error (one with a SQLSTATE) only ever describes the
        # caller's own SQL, and withholding *that* made the tool unusable for
        # the one thing it exists to do: fix your query. See query.server_error
        # for exactly where the line falls. A StatementError's own text names
        # only a position in the caller's input, so it survives both modes.
        position = f": {e}" if isinstance(e, query.StatementError) else ""
        if in_system_mode():
            detail = query.server_error(e)
            fail(f"Query failed{position}: {detail}" if detail
                 else f"Query failed{position}")
        if position:
            cause = e.__cause__ or e.__context__
            detail = f": {cause}" if cause else ""
            print(f"Query failed{position}{detail}", file=sys.stderr)
        else:
            print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
```

**Step 4: Run to verify pass**

Run: `python -m pytest tests/test_multi_output.py -v`
Expected: 12 PASS. Then `python -m pytest` — full suite green (the existing
error-path tests in `test_error_disclosure.py` pin the non-multi wording;
`position` is empty there, so nothing changes).

**Step 5: Commit**

```bash
git add db_core/commands/exec.py tests/test_multi_output.py
git commit -m "feat: --multi flag — per-statement output, json envelope, no-flag hint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs — help epilog, README, spec note

**Files:**
- Modify: `db_core/commands/exec.py` (epilog in `build_parser`, ~line 45)
- Modify: `README.md`

**Step 1: Add an epilog example**

In `build_parser`'s epilog, after the `-f query.sql` line:

```python
               f'  {name} --dev --multi -f migration.sql   show every statement\'s result\n'
```

**Step 2: Verify help renders**

Run: `python -m pytest tests/test_cli.py -v` (help-content tests still pass) and eyeball:
`python -c "import sys; sys.argv=['execute-db','--help']; import os; os.environ['EXECUTE_DB_NO_SYSTEM']='1'; from execute_db import cli; cli.main()" | grep -A1 multi`
Expected: the `--multi` flag help and the epilog example both appear.

**Step 3: README**

Add a short section after the transaction paragraph (README.md line 5's
neighborhood), matching the README's voice:

```markdown
### Multi-statement scripts (`--multi`)

A script with several statements (`BEGIN; UPDATE ...; SELECT ...; COMMIT;`) runs fine without any flag, but the wire protocol only reports the **last** statement's result — so a migration appears to print nothing. Pass `--multi` and the script is split client-side (a real lexer: dollar-quoted function bodies, comments, and `';'` inside strings are all handled) with every statement still in the **same single transaction**, and each statement's result is shown:

    execute-db --dev --multi -f migration.sql            # headed blocks per SELECT
    execute-db --dev --multi -o json -f migration.sql    # one object per statement

Under `-o json`/`-o jsonl` every statement appears — `{"statement": 2, "kind": "rows", ...}` — so a script can assert what ran without parsing stderr. `-o csv`/`-o list` cannot express multiple result sets and are rejected under `--multi`; without `--multi` they still export the script's final statement, unchanged. On failure everything rolls back and the error names the position: `statement 3 of 7 failed: ...`.

`explore-db` supports `--multi` identically (still read-only, enforced by the server per statement).
```

**Step 4: Full suite + commit**

Run: `python -m pytest`
Expected: all PASS.

```bash
git add db_core/commands/exec.py README.md
git commit -m "docs: document --multi in help epilog and README

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Final verification

**Step 1:** `python -m pytest -q` — entire suite green.

**Step 2:** Manual smoke against a scratch database if one is configured (optional — ask the user; do NOT invent credentials):

```bash
execute-db --dev --multi "CREATE TEMP TABLE _m(x int); INSERT INTO _m VALUES (1),(2); SELECT * FROM _m; DROP TABLE _m"
execute-db --dev --multi -o json "SELECT 1 AS a; SELECT 2 AS b"
execute-db --dev "SELECT 1; SELECT 2"     # expect the stderr hint
```

**Step 3:** The `specs/` tree (see `Skill: spec-management:manage-specs`) now lags the code: `ARCHITECTURE.md` describes single-execute. Note this to the user as a follow-up rather than editing specs ad hoc — the spec skill owns that workflow.
