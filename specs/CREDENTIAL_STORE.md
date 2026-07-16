---
title: Credential Store
summary: The per-app on-disk store of database environments — one `.env.<alias>` file per environment, optionally AES-256-GCM encrypted at rest, discovered by glob rather than indexed.
intent: |
  A CLI that runs SQL needs somewhere to keep connection URLs, and a connection
  URL is a live credential. This store is the answer: one file per environment,
  owner-only, written atomically, optionally encrypted under a password that
  only a terminal can supply. It exists to keep credentials off argv and out of
  scripts' reach, to make "what environments do I have?" a fact about the
  filesystem rather than an index that can disagree with it, and to keep each
  front-end's credentials in its own directory so read-only access cannot be
  replayed as read/write.
parent: ARCHITECTURE.md
children: []
sources:
  - db_core/core/store.py
  - db_core/core/crypto.py
  - db_core/commands/config.py
  - db_core/commands/password.py
tags: [credentials, storage, encryption]
---

# Credential Store

## The shape of it

An environment is not a record in a database or a stanza in a config file. An
environment **is** a file: `<config dir>/.env.<alias>`, holding one
`DATABASE_URL=...` line, either as plaintext or as an encrypted blob. The alias
is the filename suffix and nothing else. `execute-db config set dev` writes
`~/.execute-db/.env.dev`, and from that moment `--dev` is a flag on every
command that takes an environment.

`db_core/core/store.py` owns the layout, discovery, and reading.
`db_core/core/crypto.py` owns the file format and the password prompts.
`db_core/commands/config.py` and `db_core/commands/password.py` are the command
layer over them — argparse and printing, no logic worth hiding.

## There is no index

`discover_envs()` globs `.env.*` in the config directory and returns the
suffixes. That is the whole of discovery. There is deliberately no
`config.json`, no manifest, no sidecar.

The reason is that an index is a second source of truth, and two sources of
truth eventually disagree. An index that lists an environment whose file is gone
produces a flag that fails on use; a file whose index entry is gone is an
environment you own and cannot see. Neither failure has a good error message,
and both are avoidable by simply not having the index: the filesystem already
answers "what exists" atomically and for free, and `ls ~/.execute-db` is a
complete, always-correct listing that needs no tool to read.

The store still knows the name `config.json` for exactly one purpose. An older
version of this tool did keep one, so `discover_envs()` checks for a leftover
and prints a note to stderr pointing at `config set`. It **never deletes it**.
That file may hold the only surviving copy of a URL from the old direct-URL
format, and a tool that silently deletes credentials on upgrade is a tool that
loses your production connection string. The note is noisy on purpose — it
prints on every invocation that touches the store — because the correct end
state is that you read the URL out, recreate the environment, and remove the
file yourself. Nothing in the current code path reads it.

Discovery re-validates every alias it finds against the same rules `config set`
enforces (see below) and skips anything that fails with a note on stderr, rather
than trusting that every file in the directory was written by this tool. It also
skips `.tmp` files (in-flight writes, see [Atomic writes](#atomic-writes)) and
the `.ephemeral` directory, which belongs to `EPHEMERAL_TOKENS.md`.

All of these notices go to **stderr**. Stdout carries data only; that rule holds
here as everywhere.

## The stores are per-app, and that is a security boundary

`config_dir()` is `<home>/<app.current().config_dirname>` — `~/.execute-db` for
the read/write front-end, `~/.explore-db` for the read-only one. See
`db_core/app.py`: the directory name is derived from the app's name, so a single
`AppSpec` decides which store a process can see, once, at startup.

This is not filing tidiness. The two front-ends are the same engine and the same
code; the only enforcement of read-only-ness is that `explore-db` opens its
connections in a `default_transaction_read_only=on` transaction. If both tools
shared one store, then any environment you created for read-only exploration —
including a passwordless one, created precisely *because* it was only for
reading — would immediately be a `--<alias>` flag on `execute-db` too, and the
read-only property would evaporate the moment someone typed the other command.
Separate stores are what make "I gave that agent read-only access" survive
contact with the read/write binary. The same reasoning extends to ephemeral
tokens: a token minted by `explore-db` lives in `explore-db`'s store and is not
a token `execute-db` can find.

**Be honest about the limit.** Nothing in the encrypted file format binds a blob
to an app — the header is a magic string and an expiry, and both tools use the
same magic. Copying `~/.explore-db/.env.analytics` into `~/.execute-db/` gives
you a working `execute-db --analytics`, and in the plain install those are your
own files, so you can. The boundary is a *default reachability* boundary, and it
becomes a real one only under the hardened install, where each store lives under
its own service user and your account cannot read either file to copy it. See
`PRIVILEGE_SEPARATION.md`.

## `_home()` reads the passwd entry, not `$HOME`

In normal operation `_home()` is `Path.home()`. In system (hardened) mode it is
`pwd.getpwuid(os.geteuid()).pw_dir` instead.

The difference matters because hardened mode is reached through a sudo rule, and
a sudo rule is an interface anyone on the box can call. The intended path is the
launcher, which invokes sudo with `-H` so `$HOME` becomes the service user's.
But an attacker is not obliged to use the intended path: they can call the sudo
rule directly, without `-H`, and leave `$HOME` pointing at a directory they
control. If `config_dir()` trusted `$HOME`, that process would run *as the
service user* against *their* store — and `config set` would happily write a new
environment there, or a planted `.env.dev` would be read as though it were
yours. Deriving the home from the kernel's idea of who we are removes the
attacker's input from the decision entirely: in system mode there is exactly one
directory this code can address, and it is the service user's.

Outside system mode `$HOME` is honoured, because outside system mode there is no
privilege gradient to attack — the process is already you.

Tests point the store elsewhere via the module-level `_dir_override`, which
short-circuits `config_dir()` ahead of both branches. It is a test seam, not a
supported configuration knob; there is no environment variable or flag that
relocates a real store.

## Aliases: `ENV_NAME_RE`, `RESERVED_NAMES`, and a known gap

An alias becomes a `--<alias>` flag on argparse parsers built at runtime, so the
alias namespace and the flag namespace are the same namespace. `ENV_NAME_RE`
(`^[A-Za-z][A-Za-z0-9_-]*$`) keeps aliases to something that can *be* a flag —
leading letter, no spaces, no dots that would confuse the `.env.` split, no `..`
that would escape the directory. `RESERVED_NAMES` then blocks the names that
would collide with something the CLI already means: subcommands (`config`,
`password`, `token`, `schema`), flags and their short forms (`file`, `f`,
`help`), and the SQL positional (`sql`).

**`RESERVED_NAMES` is hand-maintained and already incomplete.** This is a real,
live gap and you should know about it before you add a flag:

- `meta`, `format`, and `no-pager` are flags on the exec path and are **not**
  reserved. An environment named `meta` makes argparse raise
  `conflicting option string: --meta` when it builds the exec parser — a
  traceback, not a message, on every subsequent invocation of the tool.
- `version` is dispatched as a bare word in `db_core/cli.py` before argparse is
  reached, and is also unreserved. An environment named `version` is not a
  crash; it is worse in one way and better in another — `--version` and
  `version` are intercepted and print the version, so the flag is silently
  shadowed rather than loudly broken.
- `schema --refresh` and `schema --max-age` add two more colliding names, scoped
  to that subparser.

Nothing was added speculatively to paper over this: the trigger requires naming
an environment exactly after a flag, and the dominant failure is loud. The right
fix is structural — derive the reserved set from the parsers themselves, or
catch `argparse.ArgumentError` and turn it into a real message naming the
offending environment — and **not** another hand-added string, which only moves
the next gap one flag to the right. Recorded in
`docs/plans/2026-07-15-schema-command.md`, "Follow-ups", item 1.

One trap while you are in there: `tests/test_config.py::test_config_set_rejects_bad_alias`
does not test what it says. Under pytest there is no TTY, so `cmd_set` fails at
the URL prompt for *any* alias, reserved or not, and the test passes regardless
of `RESERVED_NAMES`. Exercise `store.validate_alias` directly.

## The URL never touches argv

`read_connection_url` in `db_core/commands/config.py` prompts for the connection
URL on the controlling terminal, with echo off, via `crypto.prompt_line`. There
is no `--url` flag. There is no way to pipe it in. A caller with no terminal
gets a hard failure that says so.

This is not prompt-for-prompting's-sake. A connection URL embeds a password, and
anything on the command line is readable from `/proc/<pid>/cmdline` by other
processes, is written verbatim into sudo's logs under the hardened install, and
lands in your shell history where it will outlive the credential. A prompt has
none of those exits. Echo is off so the value is absent from the screen and from
scrollback too, which is also why `prompt_line` strips bracketed-paste escape
markers: with echo off, the terminal (or tmux) delivers `\x1b[200~` / `\x1b[201~`
around a pasted value as ordinary input bytes, and they would otherwise be
silently baked into the stored URL.

Because the value is invisible while typing, the prompt loop checks the scheme,
echoes a password-redacted preview (`console.redact_url`, which masks only the
userinfo password and preserves everything else verbatim), and asks for
confirmation. A declined preview or a bad scheme re-prompts rather than failing,
so a mis-paste costs a retry instead of a round trip through `config rm`.

The same discipline governs password entry. `crypto.prompt_password` reads from
`/dev/tty`, never stdin — stdin may be carrying piped SQL, and requiring a real
terminal is precisely what stops a non-interactive caller from supplying a
password programmatically. There is no environment variable and no flag for it.
The delegated path for unattended access is an ephemeral token
(`EPHEMERAL_TOKENS.md`), and `read_env_text` says so in its error message when
it finds an encrypted environment and no terminal.

## Encryption at rest: what it buys, and what it does not

A password is **optional**, at `config set` and forever after. Leave the prompt
blank and the environment is written in plaintext. This is true in every mode,
including the hardened install; `read_env_text`'s docstring is the precise
statement of it and is worth reading before you change anything here.

What encryption buys is a **per-use password gate**, plus ciphertext at rest.
The gate is the substantive part: an encrypted environment cannot be used
without a human at a terminal, so a script or a coding agent running under your
account cannot run its queries or read its URL. At rest, the file is useless to
backups, to disk forensics, and to anything that can read the directory but
cannot ask you for the password.

What encryption does **not** buy:

- **It is not privilege separation.** Under the plain install, a same-user
  process can read the CLI code you type your password into. Client-side crypto
  cannot beat a same-user adversary; that is `PRIVILEGE_SEPARATION.md`'s job.
- **It is not what makes the hardened install safe.** Under hardening, the file
  lives under the service user and your account cannot read it *either way*. A
  plaintext env there is not an exposed file — it is an env with no password
  gate, so anyone who can invoke the launcher (including an agent running as
  you) can run its queries without a prompt. That is a legitimate choice for a
  read-only `explore-db` env, and a poor one for production write access.
- **It is not revocation.** This is the one to internalize. A ciphertext cannot
  refuse to be decrypted, and encryption cannot un-know a URL that has already
  been read. Changing or adding a password re-encrypts a file; it does not
  invalidate a copy someone already took, and it does not invalidate the
  database credential inside it. The only real cutoff is server-side: rotate the
  database password, or issue roles with `VALID UNTIL`. Nothing in this store
  should ever be described to a user as revoking access.

The format itself is documented at the top of `db_core/core/crypto.py`: a magic
string, an 8-byte expiry, an scrypt salt, an AES-GCM nonce, and the ciphertext.
The header is authenticated as additional data, so a tampered expiry fails to
decrypt. Environment files always carry expiry `0` (never expires) — the field
exists for tokens, which share this format (`EPHEMERAL_TOKENS.md`).
`is_encrypted` is a five-byte magic check and nothing more; it returns `False`
on any `OSError`, so an unreadable file is treated as plaintext and then fails
at the read, surfacing as a generic message from `cli.main`'s `OSError` handler
(see `ERROR_DISCLOSURE.md`).

There is no password recovery, by construction. The recovery procedure is
`config set` again.

## Atomic writes

`write_encrypted` and `write_plaintext` do the same dance: write a sibling
`.tmp`, `chmod` it to `0600`, then `Path.replace` it over the target. The
containing directory is created `0700` on first use by `config set`.

Three things fall out of the ordering, and all three are the point:

1. **No permissions window.** The file is chmodded to `0600` before it exists at
   its real name, so there is no instant at which a live credential sits on disk
   at whatever the ambient umask decided. Writing in place and chmodding after
   would leave exactly that window.
2. **No torn file.** `replace` is atomic on POSIX, so a concurrent reader — or a
   process that dies mid-write — sees either the complete old environment or the
   complete new one. Truncating the real file and writing into it means a crash
   leaves a half-written credential that decrypts to nothing and is
   indistinguishable from corruption.
3. **The `.tmp` name is why `discover_envs` skips `.tmp`.** An in-flight write
   is not an environment, and it must not briefly become a `--<alias>` flag.

Note the honest limits: there is no `fsync` before the `replace`, so this is
atomic with respect to other processes, not durable against power loss.
`write_plaintext` writes a live credential and is created `0600` for exactly
that reason — "unencrypted" is not "unimportant".

`password set` (in `db_core/commands/password.py`) deliberately does **not** use
`write_encrypted`, and the reason is subtle enough to preserve. It must wipe the
plaintext original, and `replace` would unlink that original silently — leaving
the plaintext blocks on disk, which is the entire thing `password set` is trying
to undo. So it writes the tmp, then `crypto.secure_wipe`s the original, then
replaces. That opens a small window where the target does not exist and only the
`.tmp` holds the credential: a process killed there leaves `.env.<alias>.tmp`
and no `.env.<alias>`, and since discovery skips `.tmp`, the environment appears
to have vanished. Recovery is to rename the `.tmp` back by hand, or to run
`config set` again. `secure_wipe` itself is best-effort — it overwrites with
random bytes, fsyncs, and unlinks, but SSD wear-levelling and copy-on-write
filesystems may retain the old blocks, and the docstring says so rather than
implying a guarantee.

## `config set` is three commands; `config rm` is deliberately blunt

`config set` **always** re-prompts for the URL and for an optional password, and
always writes a fresh file. That single behaviour covers create, edit-the-URL,
reset-a-forgotten-password, and drop-encryption (leave the password blank) — one
code path, no modes, and no "are you sure" branching over a store whose entire
state is one file. It is also the documented answer to "I forgot the password",
because there is no recovery and there should not be a second mechanism
pretending otherwise. It prints `Creating` or `Replacing` so you know which of
the two you just did.

`config rm` securely wipes the file, then does two things that look
disproportionate and are not:

- **It revokes every outstanding token, not just that environment's.** Token
  files are self-contained encrypted URL snapshots and carry no environment
  identity — there is no field that says "this came from `dev`". Picking out
  "its" tokens is therefore not possible without decrypting each one, which
  needs each token's secret, which the store does not have. So it revokes all of
  them and tells you how many. See `EPHEMERAL_TOKENS.md` for what a token is.
- **It clears the whole schema cache.** Cache entries are keyed by a hash of the
  connection URL, so `rm` cannot identify "its" entry without first decrypting
  the environment — which would mean prompting for a password in order to delete
  something. The cache is cheap to rebuild and is not a credential, so clearing
  all of it is the right trade. See `SCHEMA_INTROSPECTION.md`.

Both blast radii come from the same root cause — **neither tokens nor cache
entries can be attributed to an environment** — and both are stated to the user
at the point of the action rather than buried. `rm` then reminds you to rotate
the database password server-side, because, per above, wiping local files is not
revocation.
