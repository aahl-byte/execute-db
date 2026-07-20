"""Split a SQL script into its top-level statements.

A single-pass character scanner that knows PostgreSQL's quoting and comment
forms but ZERO SQL keywords: `;` is a statement boundary only in plain text,
and everything inside '...' / E'...' / "..." / $tag$...$tag$ / -- / /* */ is
opaque. Embedded BEGIN/COMMIT are not special — the server enforces their
semantics (see docs/plans/2026-07-20-multi-statement-design.md).

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
