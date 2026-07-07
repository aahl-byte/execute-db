import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

from . import crypto

CONFIG_DIR = Path.home() / ".execute-db"
CONFIG_FILE = CONFIG_DIR / "config.json"
EPHEMERAL_DIR = CONFIG_DIR / ".ephemeral"

DEFAULT_ENVIRONMENTS = ["dev", "staging", "production"]

# Environments are defined by config.json keys; each key becomes an --<env> flag.
ENV_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RESERVED_NAMES = {"password", "token", "file", "f", "help", "sql"}


def config_environments(config: dict) -> list:
    envs = []
    for name in config:
        if name in RESERVED_NAMES or not ENV_NAME_RE.match(name):
            print(f"Ignoring invalid environment name in {CONFIG_FILE}: {name!r}", file=sys.stderr)
            continue
        envs.append(name)
    if not envs:
        fail(f"No valid environments defined in {CONFIG_FILE}")
    return envs


def fail(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


def init_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    CONFIG_FILE.write_text(json.dumps(
        {e: f".env.{e}" for e in DEFAULT_ENVIRONMENTS}, indent=2,
    ) + "\n")

    for env in DEFAULT_ENVIRONMENTS:
        env_file = CONFIG_DIR / f".env.{env}"
        env_file.write_text(f"DATABASE_URL=postgresql://user:password@host:5432/dbname\n")

    print(f"Created default config at: {CONFIG_DIR}")
    print(f"Update your connection strings before running queries:")
    print(f"  {CONFIG_FILE}")
    for env in DEFAULT_ENVIRONMENTS:
        print(f"  {CONFIG_DIR / f'.env.{env}'}")
    sys.exit(0)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        init_config()
    return json.loads(CONFIG_FILE.read_text())


def env_file_path(config: dict, env: str):
    """Return the env's .env file path, or None if configured as a direct URL."""
    entry = config[env]
    if entry.startswith("postgresql://") or entry.startswith("postgres://"):
        return None
    return CONFIG_DIR / entry


def read_env_text(env: str, env_path: Path) -> str:
    """Read an env file's contents, decrypting (interactively) if encrypted."""
    data = env_path.read_bytes()
    if not crypto.is_encrypted(env_path):
        return data.decode()

    try:
        password = crypto.prompt_password(f"Password for '{env}': ")
    except crypto.NoTTYError:
        fail(
            f"Environment '{env}' is encrypted; run interactively or use an "
            f"ephemeral token (execute-db token create --{env} --ttl 2h)"
        )
    try:
        return crypto.decrypt(data, password).decode()
    except crypto.DecryptionError as e:
        fail(str(e))


def url_from_env_text(text: str, source) -> str:
    values = dotenv_values(stream=StringIO(text))
    url = values.get("DATABASE_URL")
    if not url:
        fail(f"DATABASE_URL not set in {source}")
    return url


def load_database_url(config: dict, env: str) -> str:
    if env not in config:
        fail(f"Environment '{env}' not found in {CONFIG_FILE}")

    env_path = env_file_path(config, env)

    # Support direct URL string or .env filename
    if env_path is None:
        return config[env]

    if not env_path.exists():
        fail(f"Env file not found: {env_path}")

    return url_from_env_text(read_env_text(env, env_path), env_path)


def write_encrypted(path: Path, blob: bytes):
    """Write an encrypted blob next to `path` and atomically move it into place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    tmp.replace(path)


def cmd_password_set(config: dict, env: str):
    path = env_file_path(config, env)
    if path is None:
        fail(f"'{env}' is a direct URL in {CONFIG_FILE}; move it into a .env file to encrypt it")
    if not path.exists():
        fail(f"Env file not found: {path}")
    if crypto.is_encrypted(path):
        fail(f"{path} is already encrypted; use `execute-db password change --{env}`")

    password = crypto.prompt_password(f"New password for '{env}': ", confirm=True)
    plaintext = path.read_bytes()
    blob = crypto.encrypt(plaintext, password)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    crypto.secure_wipe(path)  # best-effort wipe of the plaintext original
    tmp.replace(path)
    print(f"Encrypted {path}")
    print("If you forget the password, delete the file and recreate it — there is no recovery.")


def cmd_password_change(config: dict, env: str):
    path = env_file_path(config, env)
    if path is None:
        fail(f"'{env}' is a direct URL in {CONFIG_FILE}; nothing to change")
    if not path.exists():
        fail(f"Env file not found: {path}")
    if not crypto.is_encrypted(path):
        fail(f"{path} is not encrypted; use `execute-db password set --{env}`")

    old = crypto.prompt_password(f"Current password for '{env}': ")
    try:
        plaintext = crypto.decrypt(path.read_bytes(), old)
    except crypto.DecryptionError as e:
        fail(str(e))

    new = crypto.prompt_password(f"New password for '{env}': ", confirm=True)
    write_encrypted(path, crypto.encrypt(plaintext, new))
    print(f"Password changed for {path}")


TTL_RE = re.compile(r"^(\d+)([smhd])$")
TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ttl(text: str) -> int:
    m = TTL_RE.match(text)
    if not m:
        fail(f"Invalid --ttl {text!r} (use e.g. 45s, 30m, 2h, 1d)")
    return int(m.group(1)) * TTL_UNITS[m.group(2)]


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def token_path(tid: str) -> Path:
    return EPHEMERAL_DIR / f".env.{tid}"


def cmd_token_create(config: dict, env: str, ttl: str):
    ttl_seconds = parse_ttl(ttl)

    # Decrypt (or read) the source env; this is where the password gate applies.
    path = env_file_path(config, env)
    if path is None:
        text = f"DATABASE_URL={config[env]}\n"
    else:
        if not path.exists():
            fail(f"Env file not found: {path}")
        text = read_env_text(env, path)

    token = secrets.token_urlsafe(16)
    tid = token_id(token)
    expiry = int(time.time()) + ttl_seconds

    EPHEMERAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_encrypted(token_path(tid), crypto.encrypt(text.encode(), token, expiry))

    print(f"Token: {token}")
    print(f"  id:      {tid}")
    print(f"  env:     {env}")
    print(f"  expires: {datetime.fromtimestamp(expiry):%Y-%m-%d %H:%M:%S} ({ttl})")
    print(f'Use it with: execute-db --token {token} "SELECT ..."')
    print("This token is shown once and cannot be recovered.")


def cmd_token_list():
    now = time.time()
    active = []
    if EPHEMERAL_DIR.is_dir():
        for p in sorted(EPHEMERAL_DIR.glob(".env.*")):
            try:
                expiry = crypto.expiry_of(p.read_bytes())
            except crypto.NotEncryptedError:
                continue
            if expiry and expiry < now:
                p.unlink()
                print(f"purged expired token {p.name.removeprefix('.env.')}", file=sys.stderr)
            else:
                active.append((p.name.removeprefix(".env."), expiry))
    if not active:
        print("No active tokens.")
        return
    for tid, expiry in active:
        print(f"{tid}  expires {datetime.fromtimestamp(expiry):%Y-%m-%d %H:%M:%S}")


def cmd_token_revoke(tid: str):
    path = token_path(tid)
    if not path.exists():
        fail(f"No token with id '{tid}' (see `execute-db token list`)")
    path.unlink()
    print(f"Revoked token {tid}")


def load_database_url_from_token(token: str) -> str:
    path = token_path(token_id(token))
    if not path.exists():
        fail("Unknown, expired, or revoked token")

    # Decrypt first: a successful decrypt authenticates the header (incl. expiry).
    try:
        text = crypto.decrypt(path.read_bytes(), token).decode()
    except crypto.DecryptionError:
        fail("Invalid token")

    expiry = crypto.expiry_of(path.read_bytes())
    if expiry and expiry < time.time():
        path.unlink()
        fail("Token expired (removed)")

    return url_from_env_text(text, path)


def run_query(database_url: str, sql: str):
    conn = psycopg2.connect(database_url, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)

            if cur.description is not None:
                # Statement returned a result set (SELECT, or ... RETURNING).
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                print(f"Columns: {columns}")
                print(f"Row count: {len(rows)}")
                result = [dict(zip(columns, row)) for row in rows]
                print(json.dumps(result, indent=2, default=str))
            elif cur.rowcount >= 0:
                # Write with no result set (INSERT/UPDATE/DELETE).
                print(f"Rows affected: {cur.rowcount}")
            else:
                # rowcount is -1 when undefined (e.g. DDL such as CREATE/ALTER).
                print("Statement executed.")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_env_flags(parser: argparse.ArgumentParser, envs: list, required: bool = True):
    group = parser.add_mutually_exclusive_group(required=required)
    for env in envs:
        group.add_argument(
            f"--{env}", dest=env_dest(env), action="store_true",
            help=f"connect to {env}",
        )
    return group


def env_dest(env: str) -> str:
    return "env_" + env.replace("-", "_")


def selected_env(args, envs: list) -> str:
    return next((e for e in envs if getattr(args, env_dest(e))), None)


def manage_main():
    """Handle the `password` and `token` management subcommands."""
    config = load_config()
    envs = config_environments(config)

    parser = argparse.ArgumentParser(
        prog="execute-db",
        description="Manage execute-db environment access.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_password = sub.add_parser("password", help="manage env file encryption passwords")
    pw_sub = p_password.add_subparsers(dest="action", required=True)
    p_set = pw_sub.add_parser("set", help="encrypt a plaintext .env file with a new password")
    add_env_flags(p_set, envs)
    p_change = pw_sub.add_parser("change", help="change the password of an encrypted .env file")
    add_env_flags(p_change, envs)

    p_token = sub.add_parser("token", help="manage ephemeral access tokens")
    tok_sub = p_token.add_subparsers(dest="action", required=True)
    p_create = tok_sub.add_parser("create", help="create a short-lived token for an environment")
    add_env_flags(p_create, envs)
    p_create.add_argument("--ttl", required=True, help="token lifetime, e.g. 30m, 2h, 1d")
    tok_sub.add_parser("list", help="list active tokens (purges expired ones)")
    p_revoke = tok_sub.add_parser("revoke", help="revoke a token by id")
    p_revoke.add_argument("id", help="token id (from `token list`)")

    args = parser.parse_args()

    try:
        if args.command == "password":
            env = selected_env(args, envs)
            if args.action == "set":
                cmd_password_set(config, env)
            else:
                cmd_password_change(config, env)
        elif args.command == "token":
            if args.action == "create":
                cmd_token_create(config, selected_env(args, envs), args.ttl)
            elif args.action == "list":
                cmd_token_list()
            else:
                cmd_token_revoke(args.id)
    except crypto.NoTTYError:
        fail("This command needs an interactive terminal to prompt for a password.")
    except crypto.CryptoError as e:
        fail(str(e))


def exec_main():
    parser = argparse.ArgumentParser(
        prog="execute-db",
        description="Execute SQL statements against configured databases.",
        epilog='examples:\n'
               '  execute-db --dev "INSERT INTO users (name) VALUES (\'Alice\')"\n'
               '  execute-db --dev -f migration.sql\n'
               '  execute-db --dev < migration.sql\n'
               '  execute-db password set --dev\n'
               '  execute-db password change --dev\n'
               '  execute-db token create --dev --ttl 2h\n'
               '  execute-db --token TOKEN "SELECT 1"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    config = load_config()
    envs = config_environments(config)
    group = add_env_flags(parser, envs)
    group.add_argument("--token", metavar="TOKEN", help="connect with an ephemeral access token")

    parser.add_argument("sql", nargs="?", help="SQL statement to execute")
    parser.add_argument("-f", "--file", help="path to a .sql file to execute")
    args = parser.parse_args()

    if args.token:
        database_url = load_database_url_from_token(args.token)
    else:
        env = selected_env(args, envs)
        database_url = load_database_url(config, env)

    if args.file:
        sql = Path(args.file).read_text()
    elif args.sql:
        sql = args.sql
    elif not sys.stdin.isatty():
        sql = sys.stdin.read()
    else:
        parser.error("provide SQL as an argument, via -f FILE, or pipe to stdin")

    try:
        run_query(database_url, sql)
    except Exception as e:
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("password", "token"):
        manage_main()
    else:
        exec_main()
