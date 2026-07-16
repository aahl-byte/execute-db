# execute-db

A CLI tool for executing SQL statements against PostgreSQL databases across multiple environments (dev, staging, production, or any others you define).

Statements run in a transaction that is **committed on success** and rolled back on error, so you can run migrations, inserts, updates, and DDL — as well as plain `SELECT`s. Handle production with care: `execute-db` has no read-only guard. If you only need to read, use its read-only sibling **[`explore-db`](#explore-db-read-only-sibling)**, which runs every query in a read-only transaction so the server rejects any write.

Connection credentials can optionally be **password-encrypted at rest**: an encrypted environment can only be used by entering its password on an interactive terminal, or via a short-lived [ephemeral token](#ephemeral-tokens). This keeps non-interactive callers (scripts, coding agents) from reading your connection strings or executing queries without your say-so.

**Contents:** 
- [Installation](#installation) 
- [explore-db (read-only sibling)](#explore-db-read-only-sibling) 
- [Setup](#setup) 
- [Usage](#usage) 
- [Schema introspection](#schema-introspection) 
- [Password protection](#password-protection) 
- [Ephemeral tokens](#ephemeral-tokens) 
- [Hardened install](#hardened-install-privilege-separation) 
- [Threat model](#threat-model)

## Installation

Requires Python 3.9+.

### Lightweight installation

```bash
pip install git+https://github.com/aahl-byte/execute-db
```

### hardened installation

**The recommended install is hardened ([privilege separation](#hardened-install-privilege-separation)).**  
- the hardened installation closes a loophole where encrypted token environment files can be copied and decrypted while the token is still live
- it installs **both** `execute-db` and `explore-db`, each with its cli and config files under its own dedicated service user, to protect reads from other user processes

```bash
curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh | sudo bash
```


### create an environment

see [setup](#setup)

## explore-db (read-only sibling)

Installing this package provides **two** console scripts from one shared engine:

| CLI | Transactions | Config directory |
| --- | --- | --- |
| `execute-db` | read/write (commit on success) | `~/.execute-db/` |
| `explore-db` | **read-only** (server rejects writes) | `~/.explore-db/` |

`explore-db` is byte-for-byte the same tool as `execute-db` — same commands (`config`, `password`, `token`, `schema`), same flags, same output formats — with exactly two differences: every query runs in a `default_transaction_read_only=on` transaction (so `INSERT`/`UPDATE`/`DELETE`/DDL fail at the server, not by SQL parsing), and it keeps its **own** environment store in `~/.explore-db/`. The separate store is deliberate: it means `execute-db` can never reach a passwordless environment you created only for read-only exploration.

```bash
explore-db config set analytics   # prompts for URL + optional password
explore-db --analytics "SELECT count(*) FROM events"
explore-db --analytics "DELETE FROM events"   # error: cannot execute DELETE in a read-only transaction
```

Everything below applies to both CLIs; substitute `explore-db` and `~/.explore-db/` where relevant.

## Setup

Create an environment with `execute-db config set <name>` — it prompts for the connection URL and an **optional** password, then writes `~/.execute-db/.env.<name>` (encrypted if you gave a password, plaintext if you left it blank):

```bash
execute-db config set dev        # prompts for the connection URL, then an optional password
execute-db --dev "SELECT 1"      # prompts: Password for 'dev':  (only if encrypted)
```

There is no `config.json` and no manual editing: an environment simply *is* a `.env.<name>` file in `~/.execute-db/`. Give a password at `config set` to encrypt it at rest, or leave it blank for a plaintext file; add or rotate a password later with [`password set`/`password change`](#password-protection).

### Config format

Each environment is one `~/.execute-db/.env.<name>` file — encrypted, or plaintext when you skip the password. Its contents (decrypted, if encrypted) hold a single `DATABASE_URL`:

```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

The URL is only ever entered at the `config set` prompt (never as a command-line argument, where it would leak via shell history, `/proc`, and sudo logs). Names must match `[A-Za-z][A-Za-z0-9_-]*`; reserved names (`token`, `config`, `schema`, `file`, `f`, `sql`, `help`, `password`) are rejected.

### Managing environments

```bash
execute-db config list          # show environments and whether each is encrypted
execute-db config set <name>    # create/replace: prompts for URL + optional password
execute-db config rm <name>     # remove it and revoke outstanding tokens
```

`config set` doubles as create, edit-URL, and password reset: it always re-prompts for the URL and an optional new password and writes a fresh file, so forgetting a password just means running it again (leave the password blank to drop encryption). `config rm` securely wipes the file, revokes **all** outstanding tokens (token files carry no environment identity, so a per-environment revoke isn't possible), and clears **all** [cached schema documents](#the-cache) (same reason) — to fully cut off a removed environment, rotate its database password server-side.

### Dynamic environments

Environments are not fixed — **every `.env.<name>` file in the store becomes a `--<name>` flag**. Run `execute-db config set dev_alt_db` and `execute-db --dev_alt_db ...` works immediately. Use this to give the same database different access types, or to reach databases beyond the usual dev/staging/production trio.

## Usage

Pick an environment with `--dev`, `--staging`, `--production` (or any configured name), then provide the SQL inline, from a file, or on stdin:

```bash
execute-db --dev "INSERT INTO users (name) VALUES ('Alice')"
execute-db --staging -f migration.sql
cat migration.sql | execute-db --production
```

Statements that return rows (`SELECT`, or `... RETURNING`) print an aligned table by default:

```
id | name  | email
-+---+------+------------------
1  | Alice | alice@example.com
2  | Bob   | bob@example.com
```

### Output formats

Use `-o`/`--format` to pick how result rows are rendered:

| format | description |
| --- | --- |
| `table` | aligned columns (default) — readable at a terminal |
| `vertical` | one `column`/`value` block per row (like psql's `\x`) — best for wide rows on a narrow terminal |
| `json` | pretty-printed array of objects |
| `jsonl` | one JSON object per line — good for streaming/piping |
| `csv` | RFC-4180 CSV with a header row |
| `list` | one row per line, columns tab-separated (bare values when single-column) |

```bash
execute-db --dev -o csv      "SELECT id, email FROM users" > users.csv
execute-db --dev -o jsonl    "SELECT * FROM events"        | jq .
execute-db --dev -o list     "SELECT email FROM users"     # one email per line
execute-db --dev -o vertical "SELECT * FROM users"         # stacked, narrow-friendly
```

**Paging for wide results.** At an interactive terminal, the `table` and `vertical` formats are piped through a pager (`$PAGER`, or `less -S`) so lines wider than the window **scroll left/right** instead of wrapping — use the arrow keys, `q` to quit. Short results that fit on one screen print directly. Piped or redirected output (`> file`, `| cmd`) and the machine formats are never paged. Pass `--no-pager` to disable paging even at a terminal.

**stdout carries result data only.** Status and metadata go to stderr, so piped output stays clean. Pass `--meta` to print a `2 rows, columns: id, name` summary to stderr for row-returning queries.

`NULL` is rendered literally (distinct from an empty string) in the text formats; JSON cell values (`jsonb`, arrays) are JSON-encoded.

Writes with no result set (`INSERT`/`UPDATE`/`DELETE`/DDL) print their outcome to **stderr** (nothing to stdout):

```
Rows affected: 1
```

## Schema introspection

`schema` prints a complete, machine-readable description of an environment's schema as one JSON document:

```bash
execute-db schema --dev > schema.json
execute-db schema --dev --refresh          # re-read it now — e.g. after a migration
execute-db schema --token <TOKEN> --meta   # unattended, with cache status on stderr
```

The document covers tables, partitioned tables, views, materialized views, and foreign tables — each with its columns (type, nullability, default, identity/generated, ordinal position, comment), constraints, indexes, triggers, and, for the two view kinds, the view definition — plus enums with their values, domains, functions with their signatures, sequences, installed extensions, and a `schema_version`. Constraints carry both the `pg_get_constraintdef` text *and* structured `columns`/`references` fields, so a consumer learns that this foreign key points at *that* table's *those* columns without parsing DDL back out of a string. It is all produced by a single catalog query, so what lands on stdout is a consistent snapshot rather than a set of moments that disagree.

**It is written for a program, not for you.** The intended reader is an external tool — an editor, a linter, a schema-browsing UI — that loads the document once per refresh and re-indexes it into its own structure for auto-complete, linting, option hints, and search. That's why the whole document is always served: there is no `--table`/`--schema` projection, because the consumer re-indexes anyway and slicing here would only hand it a subset to work around; and there is no `-o` format, because every renderer [above](#output-formats) is row-shaped and none of them fits a nested document. Only the JSON goes to stdout, never paged, so it pipes straight into a parser or redirects into a file. Expect megabytes — the development database this was built against (2,127 relations, 31,612 columns, 36 schemas) produces 11.7 MB.

Introspection **always runs in a read-only transaction**, even under `execute-db`: it has no reason to ever write, so it is structurally incapable of it rather than trusting a flag. It also adds no new reach — anyone who can run `execute-db --dev "SELECT ..."` can already read `pg_catalog` and `information_schema` for themselves. This is a convenience wrapper over statements the caller is already authorized to run.

### The cache

A schema only moves when someone migrates it, and re-introspecting costs seconds, so the result is cached. On the database above a cold run takes about 3s and a cache hit about 0.2s: what's cached is the exact bytes Postgres returned, so a hit is a copy to stdout with no parse and no re-serialize on the way through.

By default a cached copy is served when it is younger than **15 minutes**; `--max-age` moves that bound, and `--refresh` ignores the cache outright:

```bash
execute-db schema --dev                 # serve the cached copy if it's younger than 15m
execute-db schema --dev --max-age 2h    # accept an older one (45s/30m/2h/1d)
execute-db schema --dev --max-age 0     # don't read the cache — but still update it
execute-db schema --dev --refresh       # the same thing, spelled plainly
execute-db schema --dev --meta          # cache status on stderr, stdout still just JSON
```

`--meta` prints `cached (age 3m)` or `refreshed in 3.2s` to **stderr**, following the same rule as the rest of the tool: stdout carries data only, so `schema --meta | jq` composes.

Entries live in `<config dir>/cache/`, one file per database, named for a **hash of the connection URL** rather than for the environment. An environment and a token that point at the same database therefore share one entry, and the URL itself never touches the disk — only its digest. The file's mtime *is* the fetch time, so there is no sidecar to keep in sync with it, and the schema version is part of the filename, so bumping it misses the old entry instead of serving a stale shape to a tool that can't tell the difference. A corrupt, truncated, or unreadable entry is a miss rather than an error — it costs one re-introspection. If the cache can't be *written*, the document still goes to stdout with a warning on stderr: caching is an optimization, serving the schema is the job.

That URL-hash keying is also why [`config rm`](#managing-environments) clears the **whole** cache rather than one entry: entries carry no environment identity, so `rm` can't pick out "its" one without decrypting the environment first — and a cache this cheap to rebuild isn't worth doing that for.

The cache is plaintext, at mode `0600` in a `0700` directory, because a schema is not a credential. In the plain install that does mean a process running as you can read your table and column names out of it, even for an environment whose `.env` file is encrypted. The [hardened install](#hardened-install-privilege-separation) puts the cache in the service user's home, where your own account can't read it either — there, as everywhere, stdout is the interface, not the file.

Examples here use `execute-db` for consistency with the rest of this README, but [`explore-db`](#explore-db-read-only-sibling) is the better home for this: introspection is read-only either way, and a tool you point at a schema rarely has any business holding write access. `explore-db schema --analytics` is the same command over the same engine, against explore-db's own store.

## Password protection

Encrypt an environment's `.env` file so it can only be used with a password:

```bash
execute-db password set --dev       # prompts for a new password (twice)
execute-db --dev "SELECT 1"         # now prompts: Password for 'dev':
execute-db password change --dev    # rotate: old password, then new
```

Details:

- Environments created with `config set` are encrypted when you supply a password (leave it blank for plaintext); `password set`/`change` exist to encrypt or rotate the password of an existing `.env` file directly.
- Files are encrypted with AES-256-GCM using a scrypt-derived key. After encryption the plaintext original is overwritten and deleted (**best-effort** — on SSDs and copy-on-write filesystems the old blocks may physically survive).
- Password prompts read from the terminal (`/dev/tty`), never from stdin — piped SQL can't be mistaken for a password, and a non-interactive caller gets a hard error pointing at ephemeral tokens instead. There is no environment-variable or flag to supply the password programmatically.
- **Forgot the password?** There is no recovery. Run `execute-db config set <name>` again to overwrite the environment with a fresh URL and password.

## Ephemeral tokens

Grant temporary, password-free access to an environment — e.g. handing a coding agent scoped access for an afternoon:

```bash
execute-db token create --dev --ttl 2h
# Token: 8YOfCttjVdI5FdUfB-X6Vw   (shown once, cannot be recovered)

execute-db --token 8YOfCttjVdI5FdUfB-X6Vw "SELECT 1"   # no tty, no password needed

execute-db token list          # active tokens (wipes expired ones)
execute-db token revoke <id>   # revoke early
execute-db token sweep         # wipe expired token files now
```

Creating a token requires the environment's password (if encrypted) — the token is a decrypted copy of the env, re-encrypted under a fresh random secret with the expiry sealed into the authenticated header. Expired tokens are refused and their files deleted; tampering with a token file's expiry invalidates it. TTL accepts `45s`, `30m`, `2h`, `1d` forms.

### Self-destructing key material

A ciphertext can't refuse to be decrypted after a deadline — anyone holding ciphertext + key can always run the math. So instead of trusting the file to disappear, half of each token's encryption key (a random **key share**) is stored *only in the kernel keyring* with a kernel-enforced TTL:

- The token file is encrypted with `token + share`; the share never touches disk.
- At expiry the **kernel destroys the share** — no user process needs to run. From that moment every copy of the token file, wherever it was taken, is permanently undecryptable, even by someone holding the token.
- A reboot (or ending your last session) also destroys the share, so **tokens do not survive a reboot** — by design for ephemeral access.
- `token revoke` destroys the share too, instantly deadening any copies made of the file.
- If the kernel keyring is unavailable, `token create` falls back to token-only encryption and prints a loud warning.

### Auto-wipe at expiry

Expired token files are wiped on the clock, even if `execute-db` is never run again:

- `token create` schedules a **transient systemd user timer** that wipes the file right at expiry.
- A persistent user timer (`execute-db-token-sweep.timer`, installed on first token creation) sweeps leftovers ~2 minutes after each boot/login, catching transient timers lost to a reboot.
- As a backstop, every `execute-db` invocation silently sweeps expired files (in case systemd was unavailable — token creation warns when the timer couldn't be scheduled).

Wipes use the same best-effort overwrite-then-delete as `password set`. If you want sweeps to run even while you're logged out, enable lingering: `loginctl enable-linger $USER`.

## Hardened install (privilege separation)

Everything above runs as *you*, so another process running under your account — a script, a coding agent — can, while a token is valid, read the secret files, read the kernel keyring share, or even edit the CLI code you type your password into. Client-side crypto can't beat a same-user adversary.

The hardened install closes that gap by moving secrets and the CLI under a dedicated service user. It runs `pip` as root against the repo, so **pin `--ref` to a commit you have reviewed** rather than tracking the moving `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh \
  | sudo bash -s -- --ref <commit-sha>
```

**It hardens both `execute-db` and `explore-db`.** This isn't optional polish: [`explore-db`](#explore-db-read-only-sibling) stores the *same* database credentials, so a plaintext explore-db store would let a same-user agent read the connection string and connect read/write directly — undoing execute-db's hardening. Each tool gets its own service user and store, which also means an `explore-db` (read-only) token is not valid for `execute-db`: read-only access you delegate can't be replayed to write.

Re-running the command upgrades in place; pass `--ref` again to move to a newer reviewed commit. **What it sets up:**

- System users **`executedb`** and **`exploredb`** own `/var/lib/execute-db/.execute-db` and `/var/lib/explore-db/.explore-db` respectively (mode `0700`). Each tool's encrypted envs and tokens move to its own store — **unreadable to your own account**. Manage them in place with `execute-db config set`/`rm` and `explore-db config set`/`rm` (each launcher runs as its service user); no installer re-run is needed to add or change an environment.
- A **root-owned frozen copy** of the package in one shared venv at `/usr/local/lib/db-cli/venv` (it provides both `execute-db` and `explore-db`) — your account can't patch the code that handles your password.
- A locked-down **sudoers** rule per tool lets you run *only* that binary as its service user (`env_reset`, no `PYTHONPATH`/`LD_*` passthrough).
- Decryption and the DB connection happen inside the service-user process, whose memory your account cannot ptrace.

In this mode each CLI refuses `-f` (pipe SQL via stdin instead), caps token TTLs at 24h, and anchors keyring shares in the service user's persistent keyring. Tokens still work for delegation — an agent you hand a token can run its queries — but can no longer copy the file or read the share. Environments may be **encrypted or plaintext**: the secret file is unreadable to your account either way, but a plaintext env has no per-use password gate, so anyone who can invoke the launcher (including an agent running as you) can run its queries without a prompt — read/write for `execute-db`, read-only for `explore-db`. Encrypt an env (`password set`) when you want that gate.

> **Use the trusted path.** To keep an agent from capturing your password as you type it, always invoke `/usr/local/bin/execute-db` / `/usr/local/bin/explore-db` (or a root-owned shell alias), **not** whatever your `PATH` resolves — `PATH` is yours to shadow, so the tool can't guarantee it for you. The auto-redirect (via a marker file) is a convenience, not a security boundary.

Reverse it any time: `sudo ./install.sh --uninstall` (or re-download and run with `--uninstall`), which restores both stores to your home directory.

## Threat model

What the encryption **does** protect:

- **Credentials at rest** — encrypted `.env` and token files are AES-256-GCM ciphertext; without the password/token they are useless, including to backups, disk forensics, and anything that reads `~/.execute-db`.
- **Non-interactive access** — password entry requires a real terminal (`/dev/tty`); a script or coding agent running as you cannot decrypt an environment or mint itself a token. The only delegated path is a token you explicitly create.
- **Expiry, locally** — the CLI refuses expired tokens, expiry is tamper-evident (sealed into the authenticated header), and expired token files are wiped on the clock by systemd timers.
- **Copied token files** — each token's key share lives only in the kernel keyring and self-destructs at expiry/reboot/revoke, so a copy of the token file (even together with the token) is undecryptable once the share is gone.

With the [hardened install](#hardened-install-privilege-separation), the same-user process is additionally blocked from reading the secret files, reading the keyring share, and tampering with the CLI code — those move to a separate user your account can't touch.

What it **cannot** protect:

- **Copy-during-validity** *(without the hardened install)* — a same-user process acting *while a token is valid* can read the keyring share as well as the file and token, capture all three, and decrypt offline forever. Privilege separation closes this; the plain install does not.
- **A legitimately-held token, during its life** — an agent you gave a token to can run that environment's queries and exfiltrate results until the token expires. That's the delegation working as intended.
- **Password capture in your own session** — anything that can read your terminal (a keylogger inside your session, injected shell hooks) can capture the password as you type it. A hardware factor (yubikey — a planned addition) is the answer there.
- **Root / memory** — root, debuggers, or anything that can read process memory during a run sees the decrypted URL.
- **Disk remanence** — the overwrite-wipe is best-effort; SSD wear-leveling and copy-on-write filesystems may retain old blocks.
- **Installer trust-on-first-use** — `curl | sudo bash` trusts the repo the first time; pin `--ref` to a reviewed commit and protect the repo.

Client-side crypto fundamentally cannot revoke knowledge. To *actually* cut off exposed credentials, act server-side: rotate the database password, or issue database roles with `VALID UNTIL` so the server itself refuses logins after a deadline.
