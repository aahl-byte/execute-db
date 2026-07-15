import pytest

from db_core import app
from db_core.core import store as store_mod
from db_core.core import system

# Every test runs as one app; default to the execute-db (read/write) identity so
# code that reads app.current() works even outside a front-end. Individual tests
# reconfigure (e.g. to the read-only explore-db spec) as needed.
EXECUTE_SPEC = app.AppSpec(name="execute-db", read_only=False, version="0.0.0-test")


@pytest.fixture(autouse=True)
def _app():
    app.configure(EXECUTE_SPEC)
    yield


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at a temp dir and return it.

    Store paths resolve through `store.config_dir()`, which honors the
    `_dir_override`; system-mode detection lives in `core.system`. Patching both
    here reaches every consumer.
    """
    d = tmp_path / ".execute-db"
    d.mkdir()
    monkeypatch.setattr(store_mod, "_dir_override", d)
    # Never let a test think it is running as the service user.
    monkeypatch.setattr(system, "in_system_mode", lambda: False)
    return d


# --- psycopg2 error fakes ----------------------------------------------------
#
# Shared by test_error_disclosure.py (which owns the disclosure rule) and
# test_schema.py (which pins that the schema command applies it). Defined once,
# here, so the provenance below governs a single definition rather than two that
# drift apart.
#
# These mirror shapes MEASURED from psycopg2 2.9 against a real PostgreSQL 16,
# not invented ones:
#
#     SELEKT 1   -> pgcode '42601', diag.message_primary
#                   'syntax error at or near "SELEKT"'
#     bad host   -> pgcode None, diag.message_primary None,
#                   str(e) 'could not translate host name "…" to address'
#
# A connection-phase failure carries no SQLSTATE even when the SERVER is the one
# that answered: a fake wire-protocol server returning a real ErrorResponse
# during startup (28P01 naming a user, 3D000 naming a database) still surfaces
# as pgcode=None, diag=None. That is what makes connection errors structurally
# incapable of satisfying query.server_error's predicate, rather than merely
# unlikely to.


class Diag:
    def __init__(self, message_primary=None, message_hint=None):
        self.message_primary = message_primary
        self.message_hint = message_hint


class ServerError(Exception):
    """A psycopg2 error raised BY the server: it has a SQLSTATE."""

    def __init__(self, pgcode, primary, hint=None, text=None):
        super().__init__(text or primary)
        self.pgcode = pgcode
        self.diag = Diag(primary, hint)


class ConnError(Exception):
    """A psycopg2 connection failure: no SQLSTATE, and leaky text."""

    def __init__(self, text):
        super().__init__(text)
        self.pgcode = None
        self.diag = Diag()
