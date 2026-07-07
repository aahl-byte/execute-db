# execute-db

A CLI tool for executing SQL statements against PostgreSQL databases across multiple environments (dev, staging, production, or any others you define).

Statements run in a transaction that is **committed on success** and rolled back on error, so you can run migrations, inserts, updates, and DDL — as well as plain `SELECT`s. Handle production with care: there is no read-only guard.

Connection credentials can be **password-encrypted at rest**: an encrypted environment can only be used by entering its password on an interactive terminal, or via a short-lived [ephemeral token](#ephemeral-tokens). This keeps non-interactive callers (scripts, coding agents) from reading your connection strings or executing queries without your say-so.

## Installation

Requires Python 3.9+.

**The recommended install is [hardened (privilege separation)](#hardened-install-privilege-separation).** On any machine where other processes run as you — coding agents especially — it's the only setup that stops them reading your credentials or tampering with the CLI you type your password into. The three steps:

```bash
# 1. get the CLI
pip install git+https://github.com/aahl-byte/execute-db

# 2. configure and ENCRYPT each environment (see Setup + Password protection below)
execute-db --dev "SELECT 1"          # writes ~/.execute-db, then edit the connection strings
execute-db password set --dev        # repeat for every environment

# 3. harden — move secrets + a frozen CLI under a dedicated service user
curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh \
  | sudo bash -s -- --ref <commit-sha>
```

See [Hardened install](#hardened-install-privilege-separation) for what step 3 sets up and why to pin `--ref`.

**Lightweight install (single-user / trusted machine only).** If nothing untrusted runs under your account, you can stop after steps 1–2 and skip hardening — credentials stay encrypted at rest, but a same-user process could read the key material while a token is live.

```bash
# development checkout
git clone https://github.com/aahl-byte/execute-db.git && pip install -e execute-db
```

## Setup

On first run, `execute-db` creates a default config directory at `~/.execute-db/` containing:

| File | Purpose |
|------|---------|
| `config.json` | Maps environment names to `.env` files (or direct connection strings) |
| `.env.dev` | Dev database connection |
| `.env.staging` | Staging database connection |
| `.env.production` | Production database connection |

To initialize manually:

```bash
execute-db --dev "SELECT 1"
```

Then edit the generated files to set your actual connection strings.

### Config format

`~/.execute-db/config.json` maps each environment to either a `.env` filename or a direct PostgreSQL URL:

```json
{
  "dev": ".env.dev",
  "staging": ".env.staging",
  "production": "postgresql://user:password@host:5432/dbname"
}
```

Each `.env` file should contain a `DATABASE_URL` variable:

```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

### Dynamic environments

Environments are not fixed — **every key in `config.json` becomes a `--<name>` flag**. Add an entry like `"dev_alt_db": ".env.dev_alt_db"` and `execute-db --dev_alt_db ...` works immediately. Use this to give the same database different access types, or to reach databases beyond the default trio.

Names must match `[A-Za-z][A-Za-z0-9_-]*`; reserved names (`token`, `file`, `f`, `sql`, `help`, `password`) are ignored with a warning.

## Password protection

Encrypt an environment's `.env` file so it can only be used with a password:

```bash
execute-db password set --dev       # prompts for a new password (twice)
execute-db --dev "SELECT 1"         # now prompts: Password for 'dev':
execute-db password change --dev    # rotate: old password, then new
```

Details:

- Files are encrypted with AES-256-GCM using a scrypt-derived key. After encryption the plaintext original is overwritten and deleted (**best-effort** — on SSDs and copy-on-write filesystems the old blocks may physically survive).
- Password prompts read from the terminal (`/dev/tty`), never from stdin — piped SQL can't be mistaken for a password, and a non-interactive caller gets a hard error pointing at ephemeral tokens instead. There is no environment-variable or flag to supply the password programmatically.
- **Forgot the password?** There is no recovery. Delete the encrypted file, recreate it with your connection string, and `password set` again.
- Environments configured as a direct URL in `config.json` can't be encrypted — move the URL into a `.env` file first.

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

The hardened install closes that gap by moving secrets and the CLI under a dedicated service user:

```bash
curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh | sudo bash
```

The install runs `pip` as root against the repo, so **pin to a commit you have reviewed** rather than tracking the moving `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh \
  | sudo bash -s -- --ref <commit-sha>
```

Re-running the command upgrades in place; pass `--ref` again to move to a newer reviewed commit.

What it sets up:

- A system user **`executedb`** owns `/var/lib/execute-db/.execute-db` (mode `0700`). Your encrypted envs and tokens move there — **unreadable to your own account**.
- A **root-owned frozen copy** of the CLI at `/usr/local/lib/execute-db/venv` — your account can't patch the code that handles your password.
- A locked-down **sudoers** rule lets you run *only* that binary as `executedb` (`env_reset`, no `PYTHONPATH`/`LD_*` passthrough).
- Decryption and the DB connection happen inside the `executedb` process, whose memory your account cannot ptrace.

In this mode the CLI **requires every environment to be encrypted** (a plaintext env would have no password gate), refuses `-f` (pipe SQL via stdin instead), caps token TTLs at 24h, and anchors keyring shares in `executedb`'s persistent keyring. Tokens still work for delegation — an agent you hand a token can run its queries — but can no longer copy the file or read the share.

> **Use the trusted path.** To keep an agent from capturing your password as you type it, always invoke `/usr/local/bin/execute-db` (or a root-owned shell alias), **not** whatever `execute-db` your `PATH` resolves — `PATH` is yours to shadow, so the tool can't guarantee it for you. The auto-redirect (via a marker file) is a convenience, not a security boundary.

Reverse it any time: `sudo ./install.sh --uninstall` (or re-download and run with `--uninstall`), which restores the store to your home directory.

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

## Usage

Pick an environment with `--dev`, `--staging`, `--production` (or any configured name), then provide SQL in one of three ways:

**Inline statement:**

```bash
execute-db --dev "INSERT INTO users (name) VALUES ('Alice')"
```

**From a file:**

```bash
execute-db --staging -f migration.sql
```

**Piped from stdin:**

```bash
cat migration.sql | execute-db --production
```

### Output

Statements that return rows (`SELECT`, or `... RETURNING`) print the column names, row count, and rows as JSON:

```
Columns: ['id', 'name', 'email']
Row count: 2
[
  {
    "id": 1,
    "name": "Alice",
    "email": "alice@example.com"
  },
  {
    "id": 2,
    "name": "Bob",
    "email": "bob@example.com"
  }
]
```

Writes with no result set (`INSERT`/`UPDATE`/`DELETE`/DDL) print the affected row count:

```
Rows affected: 1
```
