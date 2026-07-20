---
title: Architecture
summary: One shared engine behind two front-ends, split into pure logic and presentation, with a process-global app identity deciding which tool it is.
intent: Explains how the codebase is shaped and why — the shared engine, the two-layer split, and the AppSpec that lets one body of code be two tools with different guarantees. Read this before any child spec; each of them assumes the vocabulary defined here.
parent: null
children:
  - CREDENTIAL_STORE.md
  - PRIVILEGE_SEPARATION.md
  - ERROR_DISCLOSURE.md
  - EPHEMERAL_TOKENS.md
  - SCHEMA_INTROSPECTION.md
  - SQL_EXECUTION.md
sources:
  - db_core/app.py
  - db_core/cli.py
  - db_core/console.py
  - db_core/core/query.py
  - db_core/core/split.py
  - execute_db/cli.py
  - explore_db/cli.py
  - pyproject.toml
tags: [architecture, layering, engine]
---

# Architecture

For *why* the tool exists and what it refuses to do, see `INTENT.md`. This spec covers how the code is arranged.

## Two tools, one engine

`pyproject.toml` declares two console scripts, `execute-db` and `explore-db`. Both are three-line front-ends (`execute_db/cli.py`, `explore_db/cli.py`) that install an `AppSpec` and hand off to `db_core.cli.main()`. Everything else — every command, every guarantee, every line of SQL — is the shared engine in `db_core/`.

The tools differ in exactly two ways, and both fall out of the spec they install:

| | `execute-db` | `explore-db` |
| --- | --- | --- |
| `read_only` | `False` | `True` — every connection opens a read-only transaction |
| store | `~/.execute-db` | `~/.explore-db` |

The read-only flag is enforced by the *server*, via `default_transaction_read_only=on`, not by inspecting SQL. The separate store is a security property rather than tidiness: it is what stops `execute-db` from reaching a passwordless environment created for read-only exploring, and what makes a delegated read-only token non-escalatable. See `CREDENTIAL_STORE.md`.

Adding a third front-end means adding an `AppSpec`, not a code path.

## AppSpec: process-global identity

`db_core/app.py` holds a frozen `AppSpec` (name, `read_only`, version) installed once by the front-end's `main()` and read everywhere via `app.current()`. Everything app-specific derives from the name: the config directory, the service user, the launcher path, and the systemd/keyring namespaces. Nothing else needs to know which tool it is.

It is a module global rather than a parameter threaded through every function, and that is deliberate: a process is exactly one app for its whole lifetime. There is no case where two specs are live at once, so passing one around would be ceremony that implies otherwise. The cost is that anything touching `app.current()` needs `app.configure()` called first — which is why `tests/conftest.py` has an autouse fixture installing the execute-db spec, and why code that does *not* read `app.current()` (such as schema introspection, which is unconditionally read-only) can be tested without one.

## The two-layer split

This is the rule that decides where new code goes, and it is the one worth internalising:

- **`db_core/core/`** is **pure logic**. It returns values, raises, or calls `console.fail()`. It does not format for a terminal, does not print, does not decide what a flag means.
- **`db_core/commands/`** is **argparse and presentation**. It parses flags, resolves credentials, calls into `core/`, and renders the result.

`db_core/cli.py` is the router: it handles the launcher redirect, top-level help and version, sweeps expired tokens as a backstop, and dispatches `argv[0]` to a command — falling through to the exec path when it is not a subcommand name. That fall-through is why subcommand names must be reserved as environment aliases (`CREDENTIAL_STORE.md` covers the mechanism and its known gaps).

`db_core/console.py` is a leaf: `fail()` (the one sanctioned fatal exit), `redact_url()`, and the tty prompts. It depends on nothing else in the package, so every layer can use it without a cycle.

Why the split earns its keep here specifically: the security-critical decisions — what leaks, what is read-only, what touches disk — all live in `core/`, where they can be tested without a terminal, a database, or a parser. The command layer is then free to be dumb. When a rule appears in both layers, that is the smell; the disclosure rule in `ERROR_DISCLOSURE.md` is defined once in `core/query.py` and merely *applied* by two commands.

## The command surface

| Command | Owns | Spec |
| --- | --- | --- |
| *(default)* | run SQL (single or `--multi`), format results | `SQL_EXECUTION.md` |
| `config` | create/list/remove environments | `CREDENTIAL_STORE.md` |
| `password` | encrypt/rotate an environment's password | `CREDENTIAL_STORE.md` |
| `token` | mint/list/revoke ephemeral access | `EPHEMERAL_TOKENS.md` |
| `schema` | dump the full schema as JSON, cached | `SCHEMA_INTROSPECTION.md` |

`config` is dispatched before any environment-flag machinery, because it must work when nothing is configured yet.

## Cross-cutting concerns

Three things do not belong to any one command, and each has its own spec:

- **`PRIVILEGE_SEPARATION.md`** — the hardened install: service users, the root-owned launcher, the sudoers rule, and system mode. Its threat model constrains every command, and any new flag that names a path must read it first.
- **`ERROR_DISCLOSURE.md`** — the rule deciding what an error may say when the caller cannot read the credential it describes. Defined in `core/query.py`, applied by the command layer.
- **`CREDENTIAL_STORE.md`** — the on-disk format and encryption at rest, which every command that needs a URL goes through.

## Dependencies

Three runtime dependencies (`psycopg2-binary`, `cryptography`, `python-dotenv`), Python 3.9+, `pytest` for development. The floor matters: `db_core/core/query.py` writes `-> "str | None"` as a *quoted* annotation rather than importing `from __future__ import annotations`, and that is the house pattern — PEP 604 unions are not runtime-valid on 3.9. Only `app.py` uses the future import.

The lean dependency list is a deliberate constraint, not an accident. It is why schema introspection is hand-written `pg_catalog` SQL rather than a reflection library (`SCHEMA_INTROSPECTION.md` explains what that bought), and why the atomic-write idiom is repeated in a few places rather than pulled behind an abstraction.

## Testing shape

The suite runs without a database. `psycopg2.connect` is faked (`tests/test_explore.py` is the idiom; the shared error fakes live in `tests/conftest.py` with a comment recording which PostgreSQL version they were measured against). The `store` fixture points the store at a temp directory and forces `in_system_mode()` false.

Tests are organised by **concern**, not by module — `test_error_disclosure.py` spans `query.py` and the commands that apply its rule; `test_config.py` reaches across six modules. Put a test where the next person will look for the behavior, not where the code happens to live.

The one thing fakes cannot prove is that the catalog SQL parses, so `tests/test_schema_integration.py` exists behind `EXECUTE_DB_TEST_URL` and skips cleanly without it. It has not yet been run: under a hardened install the URL is unreadable from a user account by design.

**A recurring hazard, learned the hard way.** This codebase has repeatedly grown tests that *looked* protective and were not — a substring assertion that matched the wrong occurrence and let a load-bearing SQL cast be deleted with a green suite; an atomicity test that a non-atomic write passed; a reserved-name test that passes regardless of the reserved list, because pytest has no tty and the code exits earlier for unrelated reasons. When a test guards something that matters, break the thing on purpose and watch it fail. A test that has never failed has never been shown to work.
