import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from db_core import cli as core_cli
from execute_db import __version__, cli

REPO_ROOT = Path(__file__).resolve().parent.parent


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


# --- a failed stdout write is one line, not a traceback ----------------------
#
# `commands/schema.py` writes the document to sys.stdout.buffer OUTSIDE its
# try, on purpose: that try reports database-disclosure errors, which a failed
# write is not. So the write lands here instead. See the plan's follow-up 3.

def test_a_failed_stdout_write_is_one_line_not_a_traceback(monkeypatch, capsys):
    def boom(argv):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(core_cli.schema_cmd, "run", boom)
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    # Exit 1, so a tool driving this gets a code it can classify.
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err == "execute-db: [Errno 28] No space left on device\n"


def test_a_broken_pipe_is_one_line_not_a_traceback(monkeypatch, capsys):
    # `schema --dev | head` delivers the bytes correctly and then blows up on
    # the flush. BrokenPipeError is an OSError, so one handler covers both.
    def boom(argv):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(core_cli.schema_cmd, "run", boom)
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == "execute-db: [Errno 32] Broken pipe\n"


def test_the_message_does_not_blame_the_write_for_a_store_failure(monkeypatch, capsys):
    """The handler is broad enough to catch a store OSError too, so it must not
    name the write.

    This is tests/test_schema.py::test_a_store_failure_is_not_relabelled_as_an
    _introspection_failure's boundary, moved one layer up: an unreadable
    .env.dev reported as "could not write the schema to stdout" is the same
    mislabelling, and the generic line is what keeps both honest.
    """
    def boom(argv):
        raise OSError(13, "Permission denied",
                      "/home/execute-db/.execute-db/.env.dev")

    monkeypatch.setattr(core_cli.schema_cmd, "run", boom)
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev"])
    with pytest.raises(SystemExit):
        cli.main()
    err = capsys.readouterr().err
    assert "write" not in err and "stdout" not in err
    assert "Permission denied" in err and ".env.dev" in err


def test_a_command_that_exits_cleanly_is_untouched(monkeypatch, capsys):
    # The handler must not turn a normal return into a failure.
    monkeypatch.setattr(core_cli.schema_cmd, "run", lambda argv: None)
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev"])
    cli.main()
    assert capsys.readouterr().err == ""


def test_console_fail_still_reports_its_own_message(monkeypatch, capsys):
    # SystemExit is not an OSError, so every command that already fails through
    # console.fail() keeps its specific message. Guards against the broad catch
    # flattening the existing commands' errors into the generic line.
    from db_core.console import fail

    def boom(argv):
        fail("No environments configured. Create one with `execute-db config set <name>`.")

    monkeypatch.setattr(core_cli.schema_cmd, "run", boom)
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "schema", "--dev"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert "No environments configured" in capsys.readouterr().err


# --- the interpreter-exit flush ----------------------------------------------

_DRIVER = textwrap.dedent("""
    import sys
    from db_core import app, cli
    from db_core.commands import schema as schema_cmd

    app.configure(app.AppSpec(name="execute-db", read_only=False, version="t"))
    size = int(sys.argv[1])  # read BEFORE sys.argv is replaced below
    # Stand in for the real command: emit a payload the way commands/schema.py
    # does, straight at the binary buffer, outside any try.
    def fake_run(argv):
        sys.stdout.buffer.write(b"x" * size)
        sys.stdout.buffer.write(b"\\n")
        sys.stdout.buffer.flush()
    schema_cmd.run = fake_run
    sys.argv = ["execute-db", "schema", "--dev"]
    cli.main()
""")


@pytest.mark.parametrize("size", [500, 200_000])
def test_a_failed_write_leaves_no_shutdown_noise(tmp_path, size):
    """Exit 1 and ONE line, whichever side of the 8KB buffer the payload falls.

    Measured, not assumed: below the buffer the explicit flush() raises but the
    buffer RETAINS the bytes, so the interpreter-exit flush of the TextIOWrapper
    fails a second time -- printing `Exception ignored in: <_io.TextIOWrapper>`
    AFTER the handler has run, and overriding exit 1 with 120. Above it the raw
    write raises with nothing retained and the exit flush never fires. Only the
    small case can catch a regression here; the large one is the control.
    """
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER)
    with open("/dev/full", "w") as full:
        proc = subprocess.run(
            [sys.executable, str(script), str(size)],
            stdout=full, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "EXECUTE_DB_NO_SYSTEM": "1",
                 "PYTHONPATH": str(REPO_ROOT)},
        )
    assert proc.returncode == 1, f"exit {proc.returncode} (120 = the exit flush refired)"
    assert proc.stderr.decode() == "execute-db: [Errno 28] No space left on device\n"
