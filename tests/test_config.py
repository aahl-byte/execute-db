import pytest

from execute_db import cli, crypto


# --- prompt_secret_line ------------------------------------------------------

def test_prompt_secret_line_rejects_empty(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass", lambda prompt="": "")
    with pytest.raises(crypto.CryptoError):
        crypto.prompt_secret_line("URL: ")


def test_prompt_secret_line_returns_value(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass", lambda prompt="": "postgresql://a")
    assert crypto.prompt_secret_line("URL: ") == "postgresql://a"


def test_prompt_secret_line_requires_tty(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: False)
    with pytest.raises(crypto.NoTTYError):
        crypto.prompt_secret_line("URL: ")


# --- config list -------------------------------------------------------------

def test_config_list_shows_state(store, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")   # plaintext
    (store / ".env.prod").write_bytes(
        crypto.encrypt(b"DATABASE_URL=postgresql://y\n", "pw"))          # encrypted
    cli.cmd_config_list()
    out = capsys.readouterr().out
    assert "dev" in out and "plaintext" in out
    assert "prod" in out and "encrypted" in out


def test_config_list_empty(store, capsys):
    cli.cmd_config_list()
    assert "No environments" in capsys.readouterr().out


# --- config set --------------------------------------------------------------

def test_config_set_creates_encrypted_env(store, monkeypatch):
    monkeypatch.setattr(crypto, "prompt_secret_line", lambda p: "postgresql://u:p@h/db")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False: "hunter2")
    cli.cmd_config_set("dev")

    path = store / ".env.dev"
    assert path.exists()
    assert crypto.is_encrypted(path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    text = crypto.decrypt(path.read_bytes(), "hunter2").decode()
    assert "DATABASE_URL=postgresql://u:p@h/db" in text
    assert not (store / ".env.dev.tmp").exists()


def test_config_set_replaces_existing(store, monkeypatch):
    (store / ".env.dev").write_bytes(b"old")
    monkeypatch.setattr(crypto, "prompt_secret_line", lambda p: "postgresql://new")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False: "pw")
    cli.cmd_config_set("dev")
    assert crypto.is_encrypted(store / ".env.dev")


def test_config_set_rejects_non_postgres_url(store, monkeypatch):
    monkeypatch.setattr(crypto, "prompt_secret_line", lambda p: "mysql://x")
    with pytest.raises(SystemExit):
        cli.cmd_config_set("dev")


@pytest.mark.parametrize("bad", ["token", "config", "1abc", "a b", "../x", ""])
def test_config_set_rejects_bad_alias(store, bad):
    with pytest.raises(SystemExit):
        cli.cmd_config_set(bad)


# --- config rm ---------------------------------------------------------------

def test_config_rm_wipes_env_and_revokes_tokens(store, monkeypatch):
    (store / ".env.dev").write_bytes(crypto.encrypt(b"DATABASE_URL=postgresql://y\n", "pw"))
    eph = store / ".ephemeral"
    eph.mkdir()
    (eph / ".env.aaaaaaaaaaaa").write_bytes(b"EXDB1tok")

    removed = []
    monkeypatch.setattr(cli.kernel_keyring, "remove",
                        lambda desc, persistent=False: removed.append(desc))
    cli.cmd_config_rm("dev")

    assert not (store / ".env.dev").exists()
    assert list(eph.glob(".env.*")) == []          # tokens revoked
    assert removed                                   # key share removal attempted


def test_config_rm_unknown_alias(store):
    with pytest.raises(SystemExit):
        cli.cmd_config_rm("nope")


# --- config dispatch ---------------------------------------------------------

def test_config_main_lists(store, monkeypatch, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "config", "list"])
    cli.config_main()
    assert "dev" in capsys.readouterr().out


# --- empty-store onboarding --------------------------------------------------

def test_exec_main_empty_store_guides_user(store, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--dev", "SELECT 1"])
    with pytest.raises(SystemExit):
        cli.exec_main()
    err = capsys.readouterr().err
    assert "config set" in err
