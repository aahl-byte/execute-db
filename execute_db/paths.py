"""Store layout: where environment files and ephemeral tokens live.

Environments are the `.env.<alias>` files in CONFIG_DIR; each becomes an
`--<alias>` flag. There is no config.json index.
"""

import os
import pwd
import re
from pathlib import Path

from . import system
from .util import fail


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
