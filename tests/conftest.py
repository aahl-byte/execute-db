import pytest

from execute_db.core import store as store_mod
from execute_db.core import system


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the store-layout globals at a temp dir and return it.

    Store paths live in `core.store`; system-mode detection lives in
    `core.system`. Every module references them by attribute, so patching them
    here reaches all consumers.
    """
    d = tmp_path / ".execute-db"
    d.mkdir()
    monkeypatch.setattr(store_mod, "CONFIG_DIR", d)
    monkeypatch.setattr(store_mod, "CONFIG_FILE", d / "config.json")
    monkeypatch.setattr(store_mod, "EPHEMERAL_DIR", d / ".ephemeral")
    # Never let a test think it is running as the service user.
    monkeypatch.setattr(system, "in_system_mode", lambda: False)
    return d
