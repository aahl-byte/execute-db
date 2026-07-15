import pytest

from db_core import cli as core_cli
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


# --- schema dispatch ---------------------------------------------------------

def test_schema_is_dispatched_to_the_schema_command(monkeypatch):
    # Mutation check: delete the `schema` branch in cli.main and this argv falls
    # through to exec_cmd.run, so `seen` stays empty and this fails.
    seen = []
    monkeypatch.setattr(core_cli.schema_cmd, "run", lambda argv: seen.append(argv))
    monkeypatch.setattr(core_cli.exec_cmd, "run",
                        lambda argv: pytest.fail(f"schema went to the exec path: {argv}"))
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev", "--refresh"])
    cli.main()
    assert seen == [["--dev", "--refresh"]]


def test_sql_still_reaches_the_exec_path(monkeypatch):
    # The other side of the dispatch: adding a branch must not swallow anything
    # that is not literally `schema`.
    seen = []
    monkeypatch.setattr(core_cli.exec_cmd, "run", lambda argv: seen.append(argv))
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--dev", "SELECT 1"])
    cli.main()
    assert seen == [["--dev", "SELECT 1"]]


def test_help_documents_the_schema_command(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--help"])
    cli.main()
    out = capsys.readouterr().out
    assert "schema --dev" in out
    assert "schema --dev --refresh" in out
    # `{name} <command> --help` lists what has one, and schema now does.
    assert "config/password/token/schema" in out


def test_help_interpolates_the_cache_default_rather_than_retyping_it(monkeypatch, capsys):
    """The 15m in the overview must come from the constant `schema --help` reads.

    Asserting "15m" appears would pass against a hardcoded literal and prove
    nothing (it was written that way first, and a hardcode mutation survived it),
    so move the constant instead: a literal cannot follow, and the overview would
    quietly contradict `schema --help` the day the default changes.
    """
    from db_core.core import schema as schema_core

    monkeypatch.setattr(schema_core, "DEFAULT_MAX_AGE_SECONDS", 45 * 60)
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--help"])
    cli.main()
    out = capsys.readouterr().out
    assert "Cached for 45m by default" in out
    assert "15m" not in out
