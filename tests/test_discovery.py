import pytest

from db_core.core import store as store_mod


def _write(store, name, body=b"DATABASE_URL=postgresql://x\n"):
    (store / name).write_bytes(body)


def test_discovers_env_files_as_aliases(store):
    _write(store, ".env.dev")
    _write(store, ".env.staging")
    assert store_mod.discover_envs() == ["dev", "staging"]


def test_ignores_non_env_and_temp_and_reserved(store):
    _write(store, ".env.dev")
    _write(store, ".env.dev.tmp")          # in-progress write
    _write(store, "config.json", b"{}")     # legacy index
    _write(store, ".env.token")             # reserved name
    (store / ".ephemeral").mkdir()          # token dir, not an env
    assert store_mod.discover_envs() == ["dev"]


def test_schema_is_a_reserved_env_name():
    # `schema` is a subcommand (cli.main dispatches it off argv[0]), so an env of
    # that name would make `execute-db schema` read ambiguously. "schema" matches
    # ENV_NAME_RE, so the only thing that can reject it here is the reserved set.
    assert "schema" in store_mod.RESERVED_NAMES
    with pytest.raises(SystemExit):
        store_mod.validate_alias("schema")


def test_an_env_file_named_schema_is_ignored(store, capsys):
    _write(store, ".env.schema")
    _write(store, ".env.dev")
    assert store_mod.discover_envs() == ["dev"]
    assert "Ignoring invalid environment file .env.schema" in capsys.readouterr().err


def test_empty_store_returns_empty(store):
    assert store_mod.discover_envs() == []


def test_env_file_path_is_conventional(store):
    assert store_mod.env_file_path("dev") == store / ".env.dev"
