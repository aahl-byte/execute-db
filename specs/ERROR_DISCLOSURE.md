---
title: Error Disclosure
summary: Over sudo, a failed statement discloses the server's own words about your SQL and nothing else; anything that could name the connection stays opaque.
intent: |
  Under a hardened install the CLI runs as a service user whose credential store
  the caller cannot read, which turns error text into a disclosure channel. The
  first hardened version closed that channel by printing a bare "Query failed"
  for everything — safe, and useless for the one thing the tool exists to do:
  fix your query. This spec records the line drawn instead, why it falls on the
  SQLSTATE, and the two subtleties (build from `diag`, walk `__context__`) that
  make it hold. It exists because breaking it is invisible: nothing fails, a
  secret just appears on someone's terminal.
parent: ARCHITECTURE.md
children: []
sources:
  - db_core/core/query.py
  - db_core/commands/exec.py
  - db_core/commands/schema.py
  - tests/test_error_disclosure.py
tags: [security, errors, disclosure, sudo]
---

# Error Disclosure

## The rule

A psycopg2 failure is one of exactly two things, and the SQLSTATE tells you which:

- **The server answered.** The exception carries a `pgcode` (SQLSTATE) and a
  `diag.message_primary` that describes the statement — `syntax error at or near
  "SELEKT"`, `relation "users" does not exist`, `permission denied for table
  pg_class`. It names nothing but the caller's own SQL. **Disclosed**, even over
  sudo.
- **The connection failed.** No SQLSTATE, and the text can echo the connection
  string: `could not translate host name "db-internal" to address`, `password
  authentication failed for user "svc_admin"`. That is precisely the leak the
  hardening exists to prevent. **Withheld** — the caller gets "Query failed" and
  nothing more.

`query.server_error(exc)` is the single decision point: it returns the
disclosable message or `None`. Everything else is plumbing.

Why the SQLSTATE and not a keyword blocklist: it is PostgreSQL's own documented
"the backend produced this" signal — not a heuristic about what the text happens
to contain, and it needs no updating when psycopg2 rewords something. See
`PRIVILEGE_SEPARATION.md` for why the service user's store is unreadable to the
caller in the first place; that is the boundary this rule protects, not one this
spec establishes.

## What is allowed to escape

`server_error` builds its message from `diag`, **never `str(exc)`**. This is not
stylistic: `str()` of a server error also carries the LINE/caret context psycopg2
appends. Restricting the output to `diag.message_primary` (plus
`diag.message_hint`, which is the server's *guidance*, not data) means the only
bytes that can cross the boundary are words the server itself chose about the
caller's own statement. "Let's just print the exception" is the whole failure
mode this shape rules out.

Both halves of the predicate are load-bearing — `pgcode` present **and**
`message_primary` non-empty — even though every measured psycopg2 error satisfies
them together. `pgcode` is the trust signal; the emptiness check stops a stub
`diag` from rendering as the string `"None"`.

A non-psycopg exception (a bug in our own code, e.g. a `ValueError` carrying a
store path) has no `pgcode`, so it falls out as withheld. Deliberate: our own
bugs must not become a disclosure channel.

## The `__context__` walk

This is the part that looks like over-engineering and is not.

Both callers — `core/query.py:run_query` and `core/schema.py:introspect` — end
their transaction in an `except`/`finally`. So when the server terminates a
backend, the process raises **twice**: the real `OperationalError` from
`execute()`, then an `InterfaceError` from the `rollback()` that tried to tidy up
after it. The **second** propagates, and it has no SQLSTATE. A naive
top-of-the-chain implementation therefore reports a bare "failed" while the
server's own plain-words complaint sits one link down in `__context__`.

So `server_error` walks the chain. Two properties keep that honest:

- **Walking cannot widen disclosure.** Every link faces the same
  `_server_message` predicate, so a connection error stays opaque wherever in the
  chain it sits. Looking *deeper* never lowers the *bar*. Pinned by
  `test_walking_the_chain_cannot_disclose_a_connection_error`.
- **The top wins.** The chain is consulted only when the top-level exception has
  nothing to say — the top *is* the error being reported, and an older error
  underneath must never replace it.

`__context__` and not `__cause__`: implicit chaining is what a raising
`except`/`finally` produces, and nothing in this codebase raises `from`.

## The evidence that a connection error cannot slip through

Worth recording because nobody will re-derive it. A reviewer stood up a fake
PostgreSQL wire-protocol server and sent real `ErrorResponse` packets during the
**startup phase** — SQLSTATE `28P01` `password authentication failed for user
"svc_admin"`, SQLSTATE `3D000` `database "proddb_internal" does not exist`. Even
though the server is the one answering, and even though a SQLSTATE is on the
wire, psycopg2 surfaces connection-phase errors as `pgcode=None` / `diag=None`.

Connection failures are therefore **structurally incapable** of satisfying the
predicate, not merely unlikely to. `tests/conftest.py` records this alongside the
psycopg2 2.9 / PostgreSQL 16 shapes the fakes mirror.

## The cycle guard

`server_error` keeps a `seen` set. Be honest about why: CPython breaks
`__context__` cycles when it chains implicitly, and psycopg2 never assigns
`__context__` by hand, so a cycle is not reachable from production — only manual
assignment produces one, which is exactly what the test does. The guard is
**defensive, not load-bearing**. It stays because this is the disclosure gate,
and two lines that guarantee termination beat depending on CPython's chaining
rules holding for every exception that ever reaches it.

## Where the split lives

In the **command layer**, not in `core`. `commands/exec.py:run()` and
`commands/schema.py:run()` each catch, then branch on `in_system_mode()`:

- hardened mode → `query.server_error(e)`, then `"Query failed: {detail}"` or
  bare `"Query failed"`;
- otherwise → print the full `str(e)`. There is no boundary to protect: the
  caller can read the store themselves, so withholding costs debuggability and
  buys nothing.

`core` stays a pure decision (`server_error` returns a string or `None`); the
command layer decides whether the boundary applies. A third command adding this
split should copy the shape, not re-derive the rule.

## Gotchas for future commands

**Keep URL resolution outside the `try`.** In both commands the store/token
lookup happens *before* the `try` that wraps the database call. Sweeping it in
would relabel a credential failure as a query failure — or, in hardened mode,
reduce a perfectly safe message like "Environment 'dev' not found" to the bare
string. `schema.py` states this in a comment; `exec.py` relies on the same
ordering without one.

The subtlety: store failures normally exit via `console.fail()` → `SystemExit`,
which `except Exception` never catches, so for the common paths the extent of the
`try` makes no observable difference. It matters only for paths that skip
`fail()` — e.g. `store.read_env_text` calling `read_bytes()` and raising `OSError`.
Don't let "it doesn't matter today" collapse the structure.

**Don't "simplify" `server_error` to `str(exc)`.** It looks equivalent on the
happy examples and is not: mutating it that way fails 7 of the 8 original tests
(commit `976a085`), because `str()` reintroduces both the LINE/caret context and
the connection string.

## Tests

`tests/test_error_disclosure.py` owns the rule; `tests/test_schema.py` pins that
the schema command applies it. The fakes (`ServerError` / `ConnError`) are
defined once in `tests/conftest.py` so the provenance note governs a single
definition rather than two that drift. The disclosed/withheld pairs, the
`diag`-not-`str()` check, the masked-rollback case, and the walk-cannot-widen
case are the four that must never be deleted.
