"""Process-global application identity for the shared database CLI engine.

Both front-ends — execute-db (read/write) and explore-db (read-only) — run this
exact same code. They differ only in the `AppSpec` they install at startup:
its `name` (which selects the config directory, service user, launcher path, and
keyring/systemd namespaces) and its `read_only` flag (enforced on every query).

A process is exactly one app for its whole lifetime, so the active spec is a
module global set once by the front-end's `cli.main()` rather than threaded
through every function. Everything downstream reads it via `current()`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppSpec:
    name: str          # user-facing command, e.g. "execute-db" / "explore-db"
    read_only: bool    # enforce read-only transactions on every connection
    version: str

    @property
    def config_dirname(self) -> str:
        # ~/.execute-db, ~/.explore-db — separate stores keep a passwordless
        # explore-db env from ever being reachable by execute-db, and vice versa.
        return f".{self.name}"

    @property
    def service_user(self) -> str:
        return self.name.replace("-", "")          # executedb, exploredb

    @property
    def launcher(self) -> str:
        return f"/usr/local/bin/{self.name}"

    @property
    def no_system_env(self) -> str:
        # EXECUTE_DB_NO_SYSTEM / EXPLORE_DB_NO_SYSTEM: opt out of the hardened
        # launcher redirect (used by tests and by the plain install).
        return self.name.upper().replace("-", "_") + "_NO_SYSTEM"


_current: "AppSpec | None" = None


def configure(spec: AppSpec) -> None:
    """Install the active app identity. Called once at process startup."""
    global _current
    _current = spec


def current() -> AppSpec:
    if _current is None:
        raise RuntimeError("no AppSpec configured; call app.configure() first")
    return _current
