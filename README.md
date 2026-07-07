# execute-db

A CLI tool for executing SQL statements against PostgreSQL databases across multiple environments (dev, staging, production, or any others you define).

Statements run in a transaction that is **committed on success** and rolled back on error, so you can run migrations, inserts, updates, and DDL — as well as plain `SELECT`s. Handle production with care: there is no read-only guard.

Connection credentials can be **password-encrypted at rest**: an encrypted environment can only be used by entering its password on an interactive terminal, or via a short-lived [ephemeral token](#ephemeral-tokens). This keeps non-interactive callers (scripts, coding agents) from reading your connection strings or executing queries without your say-so.

## Installation

Requires Python 3.9+.

```bash
pip install git+https://github.com/aahl-byte/execute-db
```

Or for development:

```bash
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

execute-db token list          # active tokens (purges expired ones)
execute-db token revoke <id>   # revoke early
```

Creating a token requires the environment's password (if encrypted) — the token is a decrypted copy of the env, re-encrypted under a fresh random secret with the expiry sealed into the authenticated header. Expired tokens are refused and their files deleted; tampering with a token file's expiry invalidates it. TTL accepts `45s`, `30m`, `2h`, `1d` forms.

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
