# explore-db

A CLI tool for running **read-only** SQL queries against PostgreSQL databases across multiple environments (dev, staging, production).

Connections are enforced as read-only via `default_transaction_read_only=on`, so you can safely explore without risk of accidental writes.

## Installation

Requires Python 3.9+.

```bash
pip install git+https://github.com/aahl-byte/explore-db
```

Or for development:

```bash
git clone https://github.com/aahl-byte/explore-db.git && pip install -e explore-db
```

## Setup

On first run, `explore-db` creates a default config directory at `~/.explore-db/` containing:

| File | Purpose |
|------|---------|
| `config.json` | Maps environment names to `.env` files (or direct connection strings) |
| `.env.dev` | Dev database connection |
| `.env.staging` | Staging database connection |
| `.env.production` | Production database connection |

To initialize manually:

```bash
explore-db --dev "SELECT 1"
```

Then edit the generated files to set your actual connection strings.

### Config format

`~/.explore-db/config.json` maps each environment to either a `.env` filename or a direct PostgreSQL URL:

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

## Usage

Pick an environment with `--dev`, `--staging`, or `--production`, then provide SQL in one of three ways:

**Inline query:**

```bash
explore-db --dev "SELECT * FROM users LIMIT 10"
```

**From a file:**

```bash
explore-db --staging -f query.sql
```

**Piped from stdin:**

```bash
cat query.sql | explore-db --production
```

### Output

Results are printed as JSON with column names and row count:

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
