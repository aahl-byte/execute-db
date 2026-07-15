import pytest

from db_core import console
from db_core.commands import config
from db_core.commands import exec as exec_cmd
from db_core.core import crypto, keyring, schema
from db_core.core import store as store_mod
from db_core.core import system


# --- prompt_line -------------------------------------------------------------

def test_prompt_line_rejects_empty(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass", lambda prompt="": "  ")
    with pytest.raises(crypto.CryptoError):
        crypto.prompt_line("URL: ")


def test_prompt_line_returns_value(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass", lambda prompt="": "postgresql://a")
    assert crypto.prompt_line("URL: ") == "postgresql://a"


def test_prompt_line_strips_bracketed_paste(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass",
                        lambda prompt="": "\x1b[200~postgresql://a\x1b[201~")
    assert crypto.prompt_line("URL: ") == "postgresql://a"


def test_prompt_line_requires_tty(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: False)
    with pytest.raises(crypto.NoTTYError):
        crypto.prompt_line("URL: ")


# --- config list -------------------------------------------------------------

def test_config_list_shows_state(store, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")   # plaintext
    (store / ".env.prod").write_bytes(
        crypto.encrypt(b"DATABASE_URL=postgresql://y\n", "pw"))          # encrypted
    config.cmd_list()
    out = capsys.readouterr().out
    assert "dev" in out and "plaintext" in out
    assert "prod" in out and "encrypted" in out


def test_config_list_empty(store, capsys):
    config.cmd_list()
    assert "No environments" in capsys.readouterr().out


# --- config set --------------------------------------------------------------

def test_config_set_creates_encrypted_env(store, monkeypatch):
    monkeypatch.setattr(crypto, "prompt_line", lambda p: "postgresql://u:p@h/db")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False, allow_empty=False:"hunter2")
    monkeypatch.setattr(console, "prompt_confirm", lambda q: True)
    config.cmd_set("dev")

    path = store / ".env.dev"
    assert path.exists()
    assert crypto.is_encrypted(path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    text = crypto.decrypt(path.read_bytes(), "hunter2").decode()
    assert "DATABASE_URL=postgresql://u:p@h/db" in text
    assert not (store / ".env.dev.tmp").exists()


def test_config_set_creates_store_dir_on_first_run(tmp_path, monkeypatch):
    # No `store` fixture: the store dir does not exist yet (fresh machine).
    d = tmp_path / ".execute-db"
    monkeypatch.setattr(store_mod, "_dir_override", d)
    monkeypatch.setattr(system, "in_system_mode", lambda: False)
    monkeypatch.setattr(crypto, "prompt_line", lambda p: "postgresql://x")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False, allow_empty=False: "pw")
    monkeypatch.setattr(console, "prompt_confirm", lambda q: True)
    config.cmd_set("dev")
    assert (d / ".env.dev").exists()
    assert oct(d.stat().st_mode)[-3:] == "700"


def test_config_set_blank_password_writes_plaintext(store, monkeypatch):
    # A blank password opts out of encryption: the env is written in plaintext.
    monkeypatch.setattr(crypto, "prompt_line", lambda p: "postgresql://u:p@h/db")
    monkeypatch.setattr(crypto, "prompt_password",
                        lambda p, confirm=False, allow_empty=False: "")
    monkeypatch.setattr(console, "prompt_confirm", lambda q: True)
    config.cmd_set("dev")

    path = store / ".env.dev"
    assert path.exists()
    assert not crypto.is_encrypted(path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert path.read_text() == "DATABASE_URL=postgresql://u:p@h/db\n"


def test_config_set_replaces_existing(store, monkeypatch):
    (store / ".env.dev").write_bytes(b"old")
    monkeypatch.setattr(crypto, "prompt_line", lambda p: "postgresql://new")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False, allow_empty=False:"pw")
    monkeypatch.setattr(console, "prompt_confirm", lambda q: True)
    config.cmd_set("dev")
    assert crypto.is_encrypted(store / ".env.dev")


def test_config_set_reprompts_on_bad_url(store, monkeypatch, capsys):
    urls = iter(["mysql://x", "postgresql://ok"])
    monkeypatch.setattr(crypto, "prompt_line", lambda p: next(urls))
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False, allow_empty=False:"pw")
    monkeypatch.setattr(console, "prompt_confirm", lambda q: True)
    config.cmd_set("dev")
    assert crypto.is_encrypted(store / ".env.dev")
    assert "must start with postgresql" in capsys.readouterr().err


def test_config_set_reprompts_when_preview_declined(store, monkeypatch):
    urls = iter(["postgresql://wrong", "postgresql://right"])
    confirms = iter([False, True])
    monkeypatch.setattr(crypto, "prompt_line", lambda p: next(urls))
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False, allow_empty=False:"pw")
    monkeypatch.setattr(console, "prompt_confirm", lambda q: next(confirms))
    config.cmd_set("dev")
    text = crypto.decrypt((store / ".env.dev").read_bytes(), "pw").decode()
    assert "postgresql://right" in text


@pytest.mark.parametrize("url,expected", [
    ("postgresql://user:secret@host:5432/db", "postgresql://user:****@host:5432/db"),
    ("postgres://u:p@h/d", "postgres://u:****@h/d"),
    ("postgresql://host/db", "postgresql://host/db"),
    ("postgresql://user@host/db", "postgresql://user@host/db"),
])
def test_redact_url(url, expected):
    assert console.redact_url(url) == expected


@pytest.mark.parametrize("bad", ["token", "config", "1abc", "a b", "../x", ""])
def test_config_set_rejects_bad_alias(store, bad):
    with pytest.raises(SystemExit):
        config.cmd_set(bad)


# --- config rm ---------------------------------------------------------------

def test_config_rm_wipes_env_and_revokes_tokens(store, monkeypatch):
    (store / ".env.dev").write_bytes(crypto.encrypt(b"DATABASE_URL=postgresql://y\n", "pw"))
    eph = store / ".ephemeral"
    eph.mkdir()
    (eph / ".env.aaaaaaaaaaaa").write_bytes(b"EXDB1tok")

    removed = []
    monkeypatch.setattr(keyring, "remove",
                        lambda desc, persistent=False: removed.append(desc))
    config.cmd_rm("dev")

    assert not (store / ".env.dev").exists()
    assert list(eph.glob(".env.*")) == []          # tokens revoked
    assert removed                                   # key share removal attempted


def test_config_rm_clears_the_schema_cache(store, capsys):
    # Entries are keyed by URL hash, not env name, so `rm` clears all of them --
    # including one belonging to an environment it is not removing.
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://u:p@host/dev\n")
    schema.write_cache(schema.cache_path("postgresql://u:p@host/dev"), b"{}")
    schema.write_cache(schema.cache_path("postgresql://u:p@host/prod"), b"{}")

    config.cmd_rm("dev")

    assert list(schema.cache_dir().glob("*.json")) == []
    assert "Cleared 2 cached schema document(s)." in capsys.readouterr().out


def test_config_rm_is_quiet_when_the_cache_is_empty(store, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")
    config.cmd_rm("dev")
    assert "cached schema" not in capsys.readouterr().out


def test_config_rm_unknown_alias(store):
    with pytest.raises(SystemExit):
        config.cmd_rm("nope")


# --- config dispatch ---------------------------------------------------------

def test_config_run_lists(store, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")
    config.run(["list"])
    assert "dev" in capsys.readouterr().out


# --- plaintext envs are allowed, even hardened ------------------------------

def test_plaintext_env_is_readable_in_system_mode(store, monkeypatch):
    # Hardened mode no longer requires encryption: a plaintext env reads back
    # without a password prompt (the file itself is protected by the service user).
    path = store / ".env.dev"
    path.write_bytes(b"DATABASE_URL=postgresql://x\n")
    monkeypatch.setattr(system, "in_system_mode", lambda: True)
    assert store_mod.read_env_text("dev", path) == "DATABASE_URL=postgresql://x\n"
    assert store_mod.load_database_url("dev") == "postgresql://x"


# --- empty-store onboarding --------------------------------------------------

def test_exec_empty_store_guides_user(store, capsys):
    with pytest.raises(SystemExit):
        exec_cmd.run(["--dev", "SELECT 1"])
    err = capsys.readouterr().err
    assert "config set" in err
