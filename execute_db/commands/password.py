"""The `password` command: encrypt an env file, or rotate its password."""

import argparse

from .flags import add_env_flags, selected_env
from ..console import fail
from ..core import crypto, store
from ..core.store import discover_envs


def cmd_set(env: str):
    path = store.env_file_path(env)
    if not path.exists():
        fail(f"Env file not found: {path}")
    if crypto.is_encrypted(path):
        fail(f"{path} is already encrypted; use `execute-db password change --{env}`")

    password = crypto.prompt_password(f"New password for '{env}': ", confirm=True)
    plaintext = path.read_bytes()
    blob = crypto.encrypt(plaintext, password)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    crypto.secure_wipe(path)  # best-effort wipe of the plaintext original
    tmp.replace(path)
    print(f"Encrypted {path}")
    print("If you forget the password, delete the file and recreate it — there is no recovery.")


def cmd_change(env: str):
    path = store.env_file_path(env)
    if not path.exists():
        fail(f"Env file not found: {path}")
    if not crypto.is_encrypted(path):
        fail(f"{path} is not encrypted; use `execute-db password set --{env}`")

    old = crypto.prompt_password(f"Current password for '{env}': ")
    try:
        plaintext = crypto.decrypt(path.read_bytes(), old)
    except crypto.DecryptionError as e:
        fail(str(e))

    new = crypto.prompt_password(f"New password for '{env}': ", confirm=True)
    store.write_encrypted(path, crypto.encrypt(plaintext, new))
    print(f"Password changed for {path}")


def build_parser(envs: list) -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="execute-db password",
        description=(
            "Encrypt an environment's .env file so it can only be used after\n"
            "entering its password on an interactive terminal.\n\n"
            "Files are encrypted with AES-256-GCM (scrypt-derived key). There is\n"
            "no password recovery: if you forget it, delete the encrypted file,\n"
            "recreate it with your connection string, and set a password again."
        ),
        epilog="examples:\n"
               "  execute-db password set --dev      # encrypt 'dev' for the first time\n"
               "  execute-db password change --dev   # rotate 'dev's password",
        formatter_class=raw,
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="{set,change}")
    p_set = sub.add_parser(
        "set",
        help="encrypt a plaintext .env file with a new password",
        description=(
            "Encrypt an environment's plaintext .env file. Prompts for a new\n"
            "password (twice) on the terminal, encrypts the file, and makes a\n"
            "best-effort wipe of the plaintext original.\n\n"
            "Afterwards, running SQL against the environment prompts for the\n"
            "password; non-interactive callers are refused (use an ephemeral\n"
            "token for that — see `execute-db token create --help`)."
        ),
        formatter_class=raw,
    )
    add_env_flags(p_set, envs)
    p_change = sub.add_parser(
        "change",
        help="change the password of an encrypted .env file",
        description=(
            "Rotate an environment's password: prompts for the current password,\n"
            "then a new one (twice). The decrypted contents never touch disk."
        ),
        formatter_class=raw,
    )
    add_env_flags(p_change, envs)
    return parser


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             "`execute-db config set <name>`.")
    args = build_parser(envs).parse_args(argv)
    try:
        env = selected_env(args, envs)
        if args.action == "set":
            cmd_set(env)
        else:
            cmd_change(env)
    except crypto.NoTTYError:
        fail("This command needs an interactive terminal to prompt for a password.")
    except crypto.CryptoError as e:
        fail(str(e))
