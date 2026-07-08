"""Privilege separation (hardened / system mode).

When installed hardened, secrets live under a dedicated service user and the
CLI runs as that user via sudo. The service user, launcher path, and config
directory are all derived from the active app's name (see `db_core.app`), so
execute-db and explore-db harden independently. See install.sh and the README.
"""

import os
import pwd
import sys
from pathlib import Path

from .. import app

MAX_SYSTEM_TTL_SECONDS = 24 * 3600  # cap on --ttl in system mode


def service_user() -> str:
    return app.current().service_user


def launcher() -> str:
    return app.current().launcher


def system_marker() -> Path:
    # redirect hint on the user side; always resolved against the real $HOME.
    return Path.home() / app.current().config_dirname / "SYSTEM"


def service_uid():
    try:
        return pwd.getpwnam(service_user()).pw_uid
    except KeyError:
        return None


def in_system_mode() -> bool:
    """True when this process IS the service user (i.e. invoked via the sudo rule)."""
    uid = service_uid()
    return uid is not None and os.geteuid() == uid


def maybe_redirect_to_launcher():
    """Convenience redirect into the hardened launcher when the store has been
    migrated to system mode. UX only — NOT a security boundary: the marker
    lives in a user-writable dir and PATH can be shadowed, so real protection
    depends on the human invoking the trusted absolute path. See the README.
    """
    if in_system_mode() or os.environ.get(app.current().no_system_env):
        return
    try:
        has_marker = system_marker().exists()
    except OSError:
        has_marker = False
    launch = launcher()
    if has_marker and os.path.exists(launch):
        os.execv(launch, [launch, *sys.argv[1:]])
