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
