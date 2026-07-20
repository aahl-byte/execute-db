---
title: SQL Execution
summary: The default command — one SQL string in, one transaction, results out; with `--multi` the string is split client-side and every statement's result is shown, including a per-statement JSON envelope for machines.
intent: |
  Running SQL is the tool's whole point, but the wire protocol has a trap: a
  multi-statement string sent as one execute() reports only the LAST statement's
  result, so a migration (`BEGIN; UPDATE ...; SELECT ...; COMMIT;`) appears to
  print nothing. This spec records the execution contract on both sides of that
  trap — the byte-identical default path plus the hint that names the trap, and
  the `--multi` path that splits the script with a real lexer while keeping the
  one-transaction guarantee. It exists because the lexer's rules and the output
  contract are easy to "simplify" into wrongness: every rule here was chosen
  against a concrete way naive splitting corrupts real SQL.
parent: ARCHITECTURE.md
children: []
sources:
  - db_core/core/query.py
  - db_core/core/split.py
  - db_core/commands/exec.py
  - tests/test_split.py
  - tests/test_multi.py
  - tests/test_multi_output.py
  - tests/test_output.py
tags: [execution, transactions, lexer, output-formats, multi-statement]
context: >
  Error text from a failed statement is governed by ERROR_DISCLOSURE.md; the
  read-only guarantee referenced here is established in ARCHITECTURE.md and
  enforced by the server, never by this layer.
---

# SQL Execution

## The transaction contract

Every invocation — with or without `--multi` — is **one connection, one
transaction**: commit once on success, roll everything back on any error.
`--multi` changes how many `execute()` calls happen inside that transaction,
never how many transactions there are. Under `explore-db` the connection opens
with `default_transaction_read_only=on`, so the server rejects writes per
statement; the multi path inherits this without any code of its own.

Embedded transaction control (`BEGIN`/`COMMIT` inside the script) is executed
**as-is, unrecognized**. An inner `BEGIN` draws a harmless server warning; an
inner `COMMIT` commits mid-script — which is exactly what the same script does
today under single-execute, so splitting changes nothing. The alternative
(lexer detects and skips them) was rejected: it teaches the lexer SQL keywords,
and the server already enforces the right semantics (INTENT.md tiebreaker #4).

## Two paths, one trap

libpq's simple-query protocol describes only the **last** statement of a
multi-statement string. `run_query` (the default) sends the string verbatim in
one `execute()` — byte-identical to what the tool always did, one round trip,
zero regression surface. `run_multi` (`--multi`) splits the string and runs
each statement on the same cursor, capturing each result as it happens; it is
the only way to see intermediate results, because the driver discards them
before the client ever could.

**The hint is the bridge.** The lexer runs on every invocation, but without
`--multi` it only *counts*. More than one statement without the flag earns a
stderr note ("N statements ran in one transaction; only the last result is
shown"). Counting cannot corrupt anything — the original string is still what
executes — and the note is what stops the next person from re-living the
"migration prints nothing" mystery this feature was built to end.

## The lexer (`core/split.py`)

A single-pass character scanner that knows PostgreSQL's quoting and comment
forms and **zero SQL keywords**. It answers one question — is this `;` inside
something? — by tracking one mode:

    normal | '...' | E'...' | "..." | $tag$...$tag$ | -- line | /* block */

The rules exist because each one breaks naive `;`-splitting on real SQL:

- `;` inside any quoted region or comment is not a boundary (`'a;b'`).
- `''` / `""` are escaped quotes, not closings.
- Backslash escapes **only** in `E'...'` strings. `standard_conforming_strings`
  is on by default, so plain `'a\'` is a *complete* string; treating `\` as an
  escape there swallows the closing quote. Conversely `E'a\''` is one string.
  The E must itself start a token — in `namE'x'` the E belongs to the
  identifier and the string is plain.
- Dollar quoting closes only on its **own tag** (`$body$ ... $$ ... $body$` —
  the inner `$$` is body text). This is the migration killer: plpgsql function
  bodies are full of semicolons. The tag grammar (no leading digit) is what
  keeps positional params like `$1` from opening a quote.
- `/* */` comments **nest** (a depth counter, not a flag); `--` runs to
  end-of-line.

Segments that are empty or comment-only after splitting are dropped (a trailing
`;`, a file-footer comment) — executing an empty string is a server error.
Unterminated constructs swallow the rest of the input rather than raising: the
text still executes, and the *server* is the authority on whether it is valid
SQL. The lexer is a pure function; `tests/test_split.py` pins every rule above
with the concrete input that breaks a lexer missing it.

## The output contract

stdout carries **result data only**; status and metadata go to stderr. Nothing
about `--multi` bends this.

Default path (unchanged, pinned by `tests/test_output.py`): row results render
in the chosen format; `count`/`ok` outcomes are one stderr line.

Under `--multi`:

- **`table` / `vertical`** — each row-producing statement is a block headed
  `-- statement N --`, blank line between blocks, paged as one document.
  Non-row statements become numbered stderr lines (`[2] Rows affected: 12`).
- **`json` / `jsonl`** — one object per executed statement, on stdout,
  including `count`/`ok` statements: a machine consumer must never need to
  parse stderr. Fields: `statement` (1-based), `preview` (the statement's own
  first line, truncated — the caller wrote it, so echoing it discloses
  nothing), `kind`, then `rowcount` or `columns`+`rows`. **The shape follows
  the flag, not the count**: one statement under `--multi` is still a
  one-element array, because consumers need a deterministic shape.
- **`csv` / `list`** — rejected up front with a pointer at json/jsonl. A csv
  with repeated headers and blank-line separators is machine-*shaped*, not
  machine-*readable*; no standard reader parses it. There is no back-compat
  cost (the combination never existed), and `-o csv` *without* `--multi` still
  exports the script's final statement exactly as before.

## Failure naming

A failing statement raises `StatementError` — `statement N of M failed` — with
the driver's error chained underneath (`raise ... from`). N and M derive only
from the caller's own input, so the position is disclosed in **both** modes;
what the underlying error may say remains ERROR_DISCLOSURE.md's decision,
applied unchanged. Statements after the failure are never sent to the server
(pinned by `tests/test_multi.py`), and the rollback covers everything before it.

## Gotchas

- **Do not "simplify" the lexer to strip-comments-then-split-on-`;`.** Comment
  detection and string detection are the same problem — you cannot know `--`
  is a comment without knowing you are not inside a string — so the "simple"
  version is this lexer minus its correctness. The test file is organised to
  make the counterexamples legible.
- **`run_query` must keep receiving the original string.** The count-only use
  of the lexer is safe *because* nothing it computes reaches `execute()` on
  the default path; `tests/test_multi_output.py` pins the string arrives
  unsplit.
- **`preview` is for humans reading the envelope, not for re-execution.** It
  is first-line-truncated; correlate by `statement` index.
