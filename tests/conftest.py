import pytest

from execute_db import paths, system


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the store-layout globals at a temp dir and return it.

    Store paths live in `paths`; system-mode detection lives in `system`. The
    domain modules reference both by attribute, so patching them here reaches
    every consumer.
    """
    d = tmp_path / ".execute-db"
    d.mkdir()
    monkeypatch.setattr(paths, "CONFIG_DIR", d)
    monkeypatch.setattr(paths, "CONFIG_FILE", d / "config.json")
    monkeypatch.setattr(paths, "EPHEMERAL_DIR", d / ".ephemeral")
    # Never let a test think it is running as the service user.
    monkeypatch.setattr(system, "in_system_mode", lambda: False)
    return d
