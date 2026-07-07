"""Privilege separation (hardened / system mode).

When installed hardened, secrets live under a dedicated service user and the
CLI runs as that user via sudo. See install.sh and the README.
"""

import os
import pwd
import sys
from pathlib import Path

SERVICE_USER = "executedb"
LAUNCHER = "/usr/local/bin/execute-db"
MAX_SYSTEM_TTL_SECONDS = 24 * 3600  # cap on --ttl in system mode

SYSTEM_MARKER = Path.home() / ".execute-db" / "SYSTEM"  # redirect hint (user side)


def service_uid():
    try:
        return pwd.getpwnam(SERVICE_USER).pw_uid
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
    if in_system_mode() or os.environ.get("EXECUTE_DB_NO_SYSTEM"):
        return
    try:
        has_marker = SYSTEM_MARKER.exists()
    except OSError:
        has_marker = False
    if has_marker and os.path.exists(LAUNCHER):
        os.execv(LAUNCHER, [LAUNCHER, *sys.argv[1:]])
