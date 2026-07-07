"""The `password` subcommand: encrypt an env file, or rotate its password."""

from . import crypto
from .envs import write_encrypted
from .paths import env_file_path
from .util import fail


def cmd_password_set(env: str):
    path = env_file_path(env)
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


def cmd_password_change(env: str):
    path = env_file_path(env)
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
    write_encrypted(path, crypto.encrypt(plaintext, new))
    print(f"Password changed for {path}")
