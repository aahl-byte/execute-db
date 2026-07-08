"""The on-disk environment store: layout, discovery, and reading.

Each environment is a `.env.<alias>` file in the config dir; each becomes an
`--<alias>` flag. There is no config.json index. This module is pure logic — it
returns values or raises/`fail`s; it does not format command output.

The config dir is per-app (`~/.execute-db`, `~/.explore-db`, ...), derived from
the active `AppSpec`. Keeping the stores separate is what prevents execute-db
from ever reaching a passwordless environment created for explore-db.
"""

import os
import pwd
import re
import sys
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

from .. import app
from . import crypto, system
from ..console import fail

# Tests point the store at a temp dir by setting this (see tests/conftest.py).
_dir_override = None


def _home() -> Path:
    # In system mode derive the home from the running uid's passwd entry, NOT
    # from $HOME: an attacker who calls the sudo rule directly without -H could
    # otherwise point the config dir at a dir they control.
    if system.in_system_mode():
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return Path.home()


def config_dir() -> Path:
    if _dir_override is not None:
        return _dir_override
    return _home() / app.current().config_dirname


def config_file() -> Path:
    return config_dir() / "config.json"


def ephemeral_dir() -> Path:
    return config_dir() / ".ephemeral"


ENV_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RESERVED_NAMES = {"password", "token", "config", "file", "f", "help", "sql"}


def env_file_path(env: str) -> Path:
    return config_dir() / f".env.{env}"


def validate_alias(alias: str):
    if alias in RESERVED_NAMES or not ENV_NAME_RE.match(alias):
        fail(f"Invalid environment name {alias!r} "
             f"(must match {ENV_NAME_RE.pattern} and not be reserved)")


def discover_envs() -> list:
    """Environments are the `.env.<alias>` files in the config dir (no config.json).

    The alias is the filename suffix. Files may be plaintext (unencrypted) or
    encrypted; `.tmp` writes, the `.ephemeral` token dir, and any leftover
    `config.json` are ignored.
    """
    cfg_dir = config_dir()
    if not cfg_dir.is_dir():
        return []
    envs = []
    for p in sorted(cfg_dir.glob(".env.*")):
        if p.name.endswith(".tmp") or not p.is_file():
            continue
        alias = p.name[len(".env."):]
        if alias in RESERVED_NAMES or not ENV_NAME_RE.match(alias):
            print(f"Ignoring invalid environment file {p.name} in {cfg_dir}",
                  file=sys.stderr)
            continue
        envs.append(alias)
    cfg_file = config_file()
    if cfg_file.exists():
        print(f"Note: {cfg_file} is no longer used; environments are read from "
              f".env.* files. A direct-URL env must be recreated with "
              f"`{app.current().name} config set <name>`.", file=sys.stderr)
    return envs


def require_encrypted(env: str):
    """In system mode, a plaintext env would let any agent with the sudo rule
    read/mint credentials with no password gate. Refuse it."""
    if system.in_system_mode():
        fail(f"Environment '{env}' is not password protected; hardened (system) "
             f"mode requires encrypted environments. Encrypt it first with "
             f"`{app.current().name} password set --{env}`.")


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
            f"ephemeral token ({app.current().name} token create --{env} --ttl 2h)"
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
             f"Create it with `{app.current().name} config set {env}`.")
    return url_from_env_text(read_env_text(env, path), path)


def write_encrypted(path: Path, blob: bytes):
    """Write an encrypted blob next to `path` and atomically move it into place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    tmp.replace(path)


def write_plaintext(path: Path, text: str):
    """Write an unencrypted env file atomically, owner-only (mode 600).

    Used when the user opts out of a password at `config set`. The file still
    holds a live credential, so it is created 600 like the encrypted form.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)
