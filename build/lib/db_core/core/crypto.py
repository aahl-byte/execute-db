"""Password encryption for the database CLI env files.

Binary file format:

    magic   b"EXDB1"                       (5 bytes)
    expiry  unix timestamp, 8-byte big-endian, 0 = never expires
    salt    16 bytes  (scrypt salt)
    nonce   12 bytes  (AES-GCM nonce)
    body    ciphertext + GCM tag

The header (magic + expiry) is authenticated as AAD, so tampering with the
expiry makes decryption fail.
"""

import getpass
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"EXDB1"
HEADER_LEN = len(MAGIC) + 8
SALT_LEN = 16
NONCE_LEN = 12
BODY_OFFSET = HEADER_LEN + SALT_LEN + NONCE_LEN

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


class CryptoError(Exception):
    """Base class for encrypted-file errors."""


class DecryptionError(CryptoError):
    """Wrong password or corrupted/tampered file."""


class NotEncryptedError(CryptoError):
    """The data is not in the recognized encrypted format."""


class NoTTYError(CryptoError):
    """A password is required but no interactive terminal is available."""


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(password.encode())


def is_encrypted(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def encrypt(plaintext: bytes, password: str, expiry: int = 0) -> bytes:
    header = MAGIC + struct.pack(">Q", expiry)
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(password, salt)
    body = AESGCM(key).encrypt(nonce, plaintext, header)
    return header + salt + nonce + body


def decrypt(blob: bytes, password: str) -> bytes:
    if blob[: len(MAGIC)] != MAGIC:
        raise NotEncryptedError("not a recognized encrypted file")

    header = blob[:HEADER_LEN]
    salt = blob[HEADER_LEN : HEADER_LEN + SALT_LEN]
    nonce = blob[HEADER_LEN + SALT_LEN : BODY_OFFSET]
    body = blob[BODY_OFFSET:]

    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, body, header)
    except InvalidTag:
        raise DecryptionError("invalid password (or corrupted file)") from None


def expiry_of(blob: bytes) -> int:
    """Read the expiry timestamp from an encrypted blob (0 = never).

    Note: reading the header does not verify it — only a successful decrypt
    proves the expiry is untampered. Callers must still check expiry against
    the authenticated header by decrypting before granting access.
    """
    if blob[: len(MAGIC)] != MAGIC:
        raise NotEncryptedError("not a recognized encrypted file")
    return struct.unpack(">Q", blob[len(MAGIC) : HEADER_LEN])[0]


def secure_wipe(path: Path) -> None:
    """Best-effort wipe: overwrite with random bytes, fsync, unlink.

    On SSDs and copy-on-write filesystems the old blocks may survive the
    overwrite; this is a best-effort measure, not a guarantee.
    """
    size = path.stat().st_size
    with path.open("r+b") as f:
        f.write(os.urandom(size))
        f.flush()
        os.fsync(f.fileno())
    path.unlink()


def _tty_available() -> bool:
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


# Bracketed-paste markers a terminal (or tmux) wraps around pasted text. When a
# paste is read with echo off, the markers are delivered as input bytes; strip
# them so they don't corrupt the value.
_PASTE_MARKERS = ("\x1b[200~", "\x1b[201~")


def prompt_line(prompt: str) -> str:
    """Read a single non-empty line from the terminal WITHOUT echo.

    Used for values that embed a credential (e.g. a DATABASE_URL) and must never
    appear on screen, in argv, shell history, sudo logs, or /proc/<pid>/cmdline.
    Reads via getpass (echo off) so a pasted value — and any bracketed-paste
    escape markers a terminal or tmux wraps around the paste — is not echoed to
    the screen or scrollback; the markers are then stripped from the value.
    """
    if not _tty_available():
        raise NoTTYError("no interactive terminal available")
    line = getpass.getpass(prompt)
    for marker in _PASTE_MARKERS:
        line = line.replace(marker, "")
    value = line.strip()
    if not value:
        raise CryptoError("value must not be empty")
    return value


def prompt_password(prompt: str = "Password: ", confirm: bool = False,
                    allow_empty: bool = False) -> str:
    """Prompt on the controlling terminal; never read the password from stdin.

    stdin may be carrying piped SQL, and requiring a real terminal is what
    keeps non-interactive callers from supplying a password programmatically.

    With `allow_empty`, an empty entry is returned as "" (the caller treats it
    as "no encryption") instead of prompting for confirmation.
    """
    if not _tty_available():
        raise NoTTYError("no interactive terminal available")

    password = getpass.getpass(prompt)
    if allow_empty and password == "":
        return ""
    if confirm:
        if getpass.getpass("Confirm password: ") != password:
            raise CryptoError("passwords do not match")
        if not password:
            raise CryptoError("password must not be empty")
    return password
