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
