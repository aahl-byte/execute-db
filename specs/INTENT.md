---
title: Intent
summary: Why execute-db exists, who it is for, and the principles that decide its arguments.
intent: Records the product philosophy — the problems this tool chose to solve, the ones it deliberately refuses, and the beliefs that settle design arguments. Everything technical lives under ARCHITECTURE.md; this is the "what and why" a reader needs before any of it makes sense.
parent: null
children: []
sources:
  - README.md
tags: [product, philosophy, intent]
---

# Intent

## The problem

You have several PostgreSQL databases — dev, staging, production — and you need to run SQL against them from a terminal, from a script, and increasingly from an agent. The usual answers are all uncomfortable. A `psql` invocation wants the connection string on the command line, where it lands in shell history, in `ps` output, and in `/proc/<pid>/cmdline`. A `.env` file in the repo is one `git add -A` away from being published. A GUI client solves the credential problem by holding your production password in a process you also use to browse the web.

`execute-db` is a narrow answer to a narrow question: **run SQL against a named environment, without the credential ever being somewhere it can leak.**

## What it is

Two console scripts over one shared engine:

- **`execute-db`** — read/write. Statements run in a transaction that commits on success and rolls back on any error, so migrations, inserts, and DDL are all fair game.
- **`explore-db`** — read-only. Byte-for-byte the same tool, with every query in a `default_transaction_read_only=on` transaction, so the *server* rejects writes rather than a regex hoping to.

You name an environment once (`config set dev`), and from then on it is a flag: `execute-db --dev "SELECT 1"`. Output formats exist for humans (aligned tables, paged) and for machines (JSON, CSV, tab-separated) — and only result rows go to stdout, so a pipe stays clean.

Beyond running statements it can dump a database's complete schema as JSON (`schema`), for an editor or linter to consume; and it can mint short-lived tokens so an unattended script can reach an encrypted environment without a password prompt.

## Who it is for

Someone who already knows SQL and does not want a client — they want their query to run against the right database with the least ceremony, and they want to be able to reason about where the credential went. Increasingly that someone is supervising an agent, which sharpens every question below: an agent is a caller you did not fully specify, running commands you did not fully read.

## What it believes

**Credentials should be hard to leak by accident, not just by attack.** Most of the design is aimed at ordinary mistakes rather than adversaries. The URL is prompted on the terminal and never accepted on argv. Environment files are mode 600 and written atomically. Errors are censored by default under privilege separation. None of this stops a determined local attacker with your uid; all of it stops a bad afternoon.

**Encryption at rest is optional, and honest about what it buys.** A password on an environment gives you a per-use gate — nothing more. Plaintext environments are supported in every mode, including hardened, because a gate you bypass with a token you pasted into a script is not security, it is theatre. See `CREDENTIAL_STORE.md`.

**Client-side crypto cannot revoke knowledge.** Once a credential has been decrypted and read, no amount of local wiping unreads it. The tool says so plainly rather than implying otherwise: to actually cut off access you rotate the password server-side, or issue roles with `VALID UNTIL`. Every "revoke" in this tool is belt-and-braces on top of that, never a substitute. See `EPHEMERAL_TOKENS.md`.

**A boundary that depends on a human doing the right thing should say so.** The hardened install is a real boundary — the store lives under a service user your account cannot read. But the convenience redirect that sends `execute-db` to the trusted launcher is *not* a boundary: the marker is in a user-writable directory and PATH can be shadowed. The code says this in its own docstring rather than letting a reader assume otherwise. Overstating a defence is worse than not having it, because it stops people from compensating. See `PRIVILEGE_SEPARATION.md`.

**Refusing to explain an error is a real cost.** The tool once withheld every failure detail under sudo, which made it useless for the one thing it exists to do: fix your query. The rule now distinguishes what the *server* said about your SQL (safe — it names nothing you did not write) from what the *connection* said (unsafe — it can echo the host and user you were not allowed to read). Security that makes the tool unusable gets worked around, and a workaround is less safe than the thing it replaced. See `ERROR_DISCLOSURE.md`.

**Read-only should mean the server says no.** `explore-db` does not parse your SQL looking for `INSERT`. It opens a read-only transaction and lets PostgreSQL enforce it. A guarantee you can state in one sentence and point at an implementation is worth more than one that depends on a parser keeping up with a language.

**Separate stores, so delegation cannot escalate.** `execute-db` and `explore-db` keep independent environment stores, which means a read-only token you hand to a colleague or an agent is not accepted by the read/write tool. The separation is the mechanism; without it, "read-only access" would be a promise rather than a property.

## What it deliberately is not

- **Not a client.** No REPL, no result grid, no query history, no connection browser. `psql` exists.
- **Not a migration framework.** It will run your migration because a migration is SQL in a transaction. It will not track, order, or roll back a series of them.
- **Not a secrets manager.** It stores connection URLs for one user on one machine. It does not sync, share, or rotate them.
- **Not a schema tool.** `schema` emits a description for a *program* to read (see `SCHEMA_INTROSPECTION.md`). It does not diff, migrate, or render one.
- **Not multi-database.** PostgreSQL only. The introspection is `pg_catalog` to its bones.

## How arguments get settled

When a change is contested, these are the tiebreakers, roughly in order:

1. **Does it move a credential somewhere it can leak?** If yes, it does not ship, however convenient.
2. **Does it make a defence sound stronger than it is?** Then fix the words, not just the code.
3. **Does it make the honest path harder than the dishonest one?** A password prompt that drives people to plaintext has made things worse.
4. **Can the server enforce it instead of us?** Prefer that. Every rule we implement is a rule we can get wrong.
5. **Would a reader have to re-derive the reasoning?** Then write it down — in a comment where it bites, not only in a commit message.
