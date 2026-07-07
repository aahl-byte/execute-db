import pytest

from execute_db import cli


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the CLI's store globals at a temp dir and return it."""
    d = tmp_path / ".execute-db"
    d.mkdir()
    monkeypatch.setattr(cli, "CONFIG_DIR", d)
    monkeypatch.setattr(cli, "CONFIG_FILE", d / "config.json")
    monkeypatch.setattr(cli, "EPHEMERAL_DIR", d / ".ephemeral")
    # Never let a test think it is running as the service user.
    monkeypatch.setattr(cli, "in_system_mode", lambda: False)
    return d
