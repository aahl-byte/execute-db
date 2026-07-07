# execute-db

A CLI tool for executing SQL statements against PostgreSQL databases across multiple environments (dev, staging, production).

Statements run in a transaction that is **committed on success** and rolled back on error, so you can run migrations, inserts, updates, and DDL — as well as plain `SELECT`s. Handle production with care: there is no read-only guard.

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

## Usage

Pick an environment with `--dev`, `--staging`, or `--production`, then provide SQL in one of three ways:

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
