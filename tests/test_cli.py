import pytest

from execute_db import __version__, cli


@pytest.fixture(autouse=True)
def _no_redirect(monkeypatch):
    # Never let main() try to exec the hardened launcher during these tests.
    monkeypatch.setenv("EXECUTE_DB_NO_SYSTEM", "1")


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--version"])
    cli.main()
    assert __version__ in capsys.readouterr().out


def test_help_flag_shows_version_and_usage(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--help"])
    cli.main()
    out = capsys.readouterr().out
    assert __version__ in out
    assert "config set" in out
    assert "token create" in out


def test_no_args_shows_help(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["execute-db"])
    cli.main()
    assert "execute-db" in capsys.readouterr().out
