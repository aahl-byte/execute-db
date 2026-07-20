# Multi-statement SQL (`--multi`) — design

## Problem

`core/query.run_query` sends the whole SQL string to one `cur.execute()`. Over
libpq's simple-query protocol the cursor describes only the **last** statement,
so a migration file (`BEGIN; UPDATE ...; SELECT ...; COMMIT;`) ends with the
cursor on `COMMIT`: `description is None`, `rowcount == -1`, and the tool prints
nothing but "Statement executed." Every intermediate result is discarded by the
driver before we see it. Both `execute-db` and `explore-db` share this path.

## Decision summary

- New `--multi` flag on the default (exec) command of both binaries. Without it,
  behavior is byte-identical to today (same single `execute()`, same one round
  trip). With it, the SQL is split client-side and each statement runs on the
  same cursor — same connection, same transaction, commit at the end, rollback
  of everything on any error. Atomicity is unchanged.
- A statement **lexer** (single-pass character scanner, no SQL keywords) always
  runs. Without `--multi` it only *counts*: if it finds more than one statement,
  stderr gets a hint that only the last statement's result is shown and that
  `--multi` exists. Counting cannot corrupt anything — the original string is
  still what gets executed.
- No `--machine` flag: `-o json` / `-o jsonl` are already the machine axis.
  Under `--multi` they emit a per-statement envelope (below). `csv` and `list`
  cannot express multiple result sets parseably, so `--multi -o csv|list` is
  rejected with an error pointing at json/jsonl. (No back-compat concern — the
  combination is new.)

## The lexer (`core/split.py`)

One pass over the text tracking a single mode:

    normal | '...' | E'...' | "..." | $tag$...$tag$ | -- line | /* block */

Rules that make naive `;`-splitting wrong, all covered by the mode machine:

- `;` inside any quoted region or comment is not a boundary.
- `''` is an escaped quote in both `'...'` and `E'...'`.
- Backslash is an escape **only** in `E'...'` strings (`standard_conforming_strings`
  is on by default): `'a\'` is a complete string; `E'a\''` is too.
- Dollar quoting with optional tag (`$$`, `$body$`); the closing delimiter must
  match the opening tag exactly. This is the migration killer — plpgsql bodies
  are full of semicolons.
- `/* */` comments **nest** in PostgreSQL: depth counter, not a flag.
- `--` comments run to end of line.

Output: the list of non-empty statements (comment-only / whitespace-only
segments dropped; a trailing `;` does not create an empty statement). The lexer
recognizes no keywords — embedded `BEGIN`/`COMMIT` are executed as-is and the
server enforces the semantics (an inner `BEGIN` warns harmlessly; an inner
`COMMIT` commits, exactly as it does today under single-execute). Pure function,
unit-tested in isolation.

## Execution (`core/query.py`)

`run_query` keeps its exact signature and behavior for the single path. New:

    run_multi(database_url, sql) -> list[StatementResult]

where `StatementResult` = the existing `QueryResult` classification (rows /
count / ok, decided per statement by the same `description` / `rowcount` logic)
plus `index` (1-based) and `preview` (first line of the statement, truncated to
~80 chars). All statements run on one cursor inside one transaction; success
commits once at the end, any failure rolls back everything.

Errors: raise with enough context for the command layer to report
`statement N of M failed`, with the detail still routed through the existing
`server_error` disclosure gate. N and M derive from the caller's own input, so
disclosing them is safe in system mode.

`explore-db` inherits everything: the read-only transaction option is set on the
connection, so the server rejects writes per statement, unchanged.

## Output (`commands/exec.py`)

Without `--multi` (default): unchanged in every format, plus the stderr hint
when the count exceeds one:

    note: 7 statements ran in one transaction; only the last result is shown.
          Re-run with --multi to see each statement's result.

With `--multi`:

- `table` / `vertical` (human): each row-producing statement's result rendered
  as today, preceded by a `-- statement 3 --` header, blank line between blocks;
  the whole thing paged as one document. Non-row statements get numbered stderr
  lines: `[2] Rows affected: 12`, `[5] Statement executed.`
- `json`: one array on stdout, one object per executed statement — shape follows
  the flag, not the count (a single statement still yields a one-element array):

      [
        {"statement": 1, "preview": "UPDATE users SET ...", "kind": "count", "rowcount": 12},
        {"statement": 2, "preview": "SELECT id, email FROM ...", "kind": "rows",
         "columns": ["id", "email"], "rows": [{"id": 1, "email": "a@b.c"}]},
        {"statement": 3, "preview": "CREATE INDEX ...", "kind": "ok"}
      ]

- `jsonl`: the same objects, one per line.
- `csv` / `list`: rejected up front (`--multi` cannot render multiple result
  sets as csv/list; use -o json or jsonl). Running *without* `--multi` already
  gives csv of the file's final statement.
- `--meta`: per row-statement, the existing `N rows, columns: ...` stderr line,
  prefixed with the statement number.

stdout stays data-only; stderr stays status-only. Pipes stay clean.

## What was considered and rejected

- **Strip comments + split on `;`** — cannot work: `;` in string literals, `--`
  inside literals, and dollar-quoted bodies all require the same mode-tracking,
  so the "simple" version is the lexer minus correctness.
- **`--machine` flag** — duplicate axis; `-o` already says it.
- **Auto-detect (no flag)** — silently changes execution strategy (1 round trip
  → N) and output shape for existing scripts. Opt-in + hint is strictly safer.
- **Lexer skips `BEGIN`/`COMMIT`** — teaches the lexer SQL keywords; server
  already enforces the right semantics (tiebreaker: prefer server enforcement).
- **Blank-line-separated csv blocks** — machine-shaped but not machine-readable;
  no standard reader parses it.

## Testing

- Lexer unit tests: every rule above, plus `'a;b'`, `E'\''`, `$tag$ ... $tag$`
  with lookalike tags, nested `/* /* */ */`, comment-only input, trailing `;`.
- `run_multi` against the mocked cursor (existing test pattern in
  `test_explore.py`): per-statement classification, one commit, rollback on
  mid-script failure, read-only option preserved.
- Output tests (existing pattern in `test_output.py`): envelope shapes for
  json/jsonl, statement headers for table/vertical, csv/list rejection, the
  no-flag hint line, single-statement `--multi` still yields a list.
