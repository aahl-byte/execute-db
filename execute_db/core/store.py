"""The on-disk environment store: layout, discovery, and reading.

Each environment is a `.env.<alias>` file in CONFIG_DIR; each becomes an
`--<alias>` flag. There is no config.json index. This module is pure logic — it
returns values or raises/`fail`s; it does not format command output.
"""

import os
import pwd
import re
import sys
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

from . import crypto, system
from ..console import fail


def _resolve_config_dir() -> Path:
    # In system mode derive the home from the running uid's passwd entry, NOT
    # from $HOME: an attacker who calls the sudo rule directly without -H could
    # otherwise point CONFIG_DIR (and thus config.json) at a dir they control.
    if system.in_system_mode():
        return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".execute-db"
    return Path.home() / ".execute-db"


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
EPHEMERAL_DIR = CONFIG_DIR / ".ephemeral"

ENV_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RESERVED_NAMES = {"password", "token", "config", "file", "f", "help", "sql"}


def env_file_path(env: str) -> Path:
    return CONFIG_DIR / f".env.{env}"


def validate_alias(alias: str):
    if alias in RESERVED_NAMES or not ENV_NAME_RE.match(alias):
        fail(f"Invalid environment name {alias!r} "
             f"(must match {ENV_NAME_RE.pattern} and not be reserved)")


def discover_envs() -> list:
    """Environments are the `.env.<alias>` files in CONFIG_DIR (no config.json).

    The alias is the filename suffix. Files may be plaintext (plain install) or
    encrypted; `.tmp` writes, the `.ephemeral` token dir, and any leftover
    `config.json` are ignored.
    """
    if not CONFIG_DIR.is_dir():
        return []
    envs = []
    for p in sorted(CONFIG_DIR.glob(".env.*")):
        if p.name.endswith(".tmp") or not p.is_file():
            continue
        alias = p.name[len(".env."):]
        if alias in RESERVED_NAMES or not ENV_NAME_RE.match(alias):
            print(f"Ignoring invalid environment file {p.name} in {CONFIG_DIR}",
                  file=sys.stderr)
            continue
        envs.append(alias)
    if CONFIG_FILE.exists():
        print(f"Note: {CONFIG_FILE} is no longer used; environments are read from "
              f".env.* files. A direct-URL env must be recreated with "
              f"`execute-db config set <name>`.", file=sys.stderr)
    return envs


def require_encrypted(env: str):
    """In system mode, a plaintext env would let any agent with the sudo rule
    read/mint credentials with no password gate. Refuse it."""
    if system.in_system_mode():
        fail(f"Environment '{env}' is not password protected; hardened (system) "
             f"mode requires encrypted environments. Encrypt it first with "
             f"`execute-db password set --{env}`.")


def read_env_text(env: str, env_path: Path) -> str:
    """Read an env file's contents, decrypting (interactively) if encrypted."""
    data = env_path.read_bytes()
    if not crypto.is_encrypted(env_path):
        require_encrypted(env)
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


def load_database_url(env: str) -> str:
    path = env_file_path(env)
    if not path.exists():
        fail(f"Environment '{env}' not found (looked for {path}). "
             f"Create it with `execute-db config set {env}`.")
    return url_from_env_text(read_env_text(env, path), path)


def write_encrypted(path: Path, blob: bytes):
    """Write an encrypted blob next to `path` and atomically move it into place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    tmp.replace(path)
