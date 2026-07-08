"""Ephemeral tokens: short-lived, password-free access to one environment.

A token is a copy of an environment's credentials re-encrypted under a fresh
random secret with the expiry sealed into the authenticated header. Half of the
key (a key share) lives only in the kernel keyring with a TTL, so at expiry (or
reboot) the share self-destructs and the token file becomes undecryptable.

Pure logic: the create/list/sweep/revoke functions return data (or wiped ids);
the command layer formats the reports and warnings for the terminal.
"""

import hashlib
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import app
from . import crypto, keyring, store, system
from ..console import fail

TTL_RE = re.compile(r"^(\d+)([smhd])$")
TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

TOKEN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class TokenResult:
    token: str
    tid: str
    env: str
    expiry: int
    ttl: str
    bound: bool       # key share stored in the kernel keyring?
    scheduled: bool   # systemd user timer scheduled to wipe at expiry?


def parse_ttl(text: str) -> int:
    m = TTL_RE.match(text)
    if not m:
        fail(f"Invalid --ttl {text!r} (use e.g. 45s, 30m, 2h, 1d)")
    seconds = int(m.group(1)) * TTL_UNITS[m.group(2)]
    if seconds <= 0:
        fail(f"Invalid --ttl {text!r}: must be greater than zero")
    if system.in_system_mode() and seconds > system.MAX_SYSTEM_TTL_SECONDS:
        fail(f"--ttl {text!r} exceeds the {system.MAX_SYSTEM_TTL_SECONDS // 3600}h maximum "
             f"in hardened (system) mode")
    return seconds


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def token_path(tid: str) -> Path:
    # tids are always derived from sha256 hexdigests; validate before building a
    # path so a user-supplied id (e.g. `token revoke ../../.env.production`)
    # cannot escape the ephemeral dir and wipe an arbitrary file.
    if not TOKEN_ID_RE.match(tid):
        fail(f"Invalid token id {tid!r}")
    return store.ephemeral_dir() / f".env.{tid}"


def share_desc(tid: str) -> str:
    return f"{app.current().name}:token:{tid}"


def token_passphrase(token: str, share: bytes) -> str:
    return f"{token}:{share.decode()}" if share else token


def cli_binary():
    """Absolute path to this app's entry point, for systemd units."""
    name = app.current().name
    candidate = Path(sys.executable).with_name(name)
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


def schedule_token_wipe(ttl_seconds: int) -> bool:
    """Schedule a transient one-shot systemd user timer to sweep at expiry.

    HOME is pinned so the sweep targets the same config dir that created the
    token. Transient timers do not survive a reboot; install_boot_sweep()
    covers that gap.
    """
    # System mode has no user session/bus; the installed system timer sweeps
    # instead. Attempting `systemd-run --user` here just fails noisily.
    if system.in_system_mode():
        return False
    exe = cli_binary()
    if not exe or not shutil.which("systemd-run"):
        return False
    cmd = [
        "systemd-run", "--user", "--collect", "--quiet",
        f"--on-active={ttl_seconds + 2}",
        "--timer-property=AccuracySec=1s",
        f"--setenv=HOME={Path.home()}",
        exe, "token", "sweep",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def install_boot_sweep():
    """Install a persistent user timer that sweeps shortly after each boot/login.

    Catches token files whose transient wipe timer was lost to a reboot.
    Written once; failures are silent (schedule_token_wipe reports the
    user-visible outcome).
    """
    if system.in_system_mode():  # the installed system timer covers this
        return
    exe = cli_binary()
    if not exe or not shutil.which("systemctl"):
        return
    name = app.current().name
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service = unit_dir / f"{name}-token-sweep.service"
    timer = unit_dir / f"{name}-token-sweep.timer"
    if service.exists() and timer.exists():
        return
    unit_dir.mkdir(parents=True, exist_ok=True)
    service.write_text(
        "[Unit]\n"
        f"Description=Wipe expired {name} ephemeral tokens\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exe} token sweep\n"
    )
    timer.write_text(
        "[Unit]\n"
        f"Description=Wipe expired {name} ephemeral tokens after startup\n\n"
        "[Timer]\n"
        "OnStartupSec=2min\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    for args in (["daemon-reload"], ["enable", "--now", timer.name]):
        subprocess.run(["systemctl", "--user", *args], capture_output=True)


def create_token(env: str, ttl: str) -> TokenResult:
    """Mint a token for `env`, persist the encrypted snapshot, and schedule its
    wipe. Prompts for the env password if it is encrypted. Returns the details
    for the caller to report; does not print."""
    ttl_seconds = parse_ttl(ttl)

    # Decrypt (or read) the source env; this is where the password gate applies.
    path = store.env_file_path(env)
    if not path.exists():
        fail(f"Env file not found: {path}")
    text = store.read_env_text(env, path)

    token = secrets.token_urlsafe(16)
    tid = token_id(token)
    expiry = int(time.time()) + ttl_seconds

    # Bind the file to a key share that lives only in the kernel keyring with a
    # TTL: at expiry (or reboot) the kernel destroys the share, and no copy of
    # the file can ever be decrypted again — even by someone holding the token.
    share = secrets.token_hex(32).encode()
    bound = keyring.store(share_desc(tid), share, ttl_seconds + 2,
                          persistent=system.in_system_mode())
    passphrase = token_passphrase(token, share if bound else None)

    store.ephemeral_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    store.write_encrypted(token_path(tid), crypto.encrypt(text.encode(), passphrase, expiry))

    install_boot_sweep()
    scheduled = schedule_token_wipe(ttl_seconds)

    return TokenResult(token=token, tid=tid, env=env, expiry=expiry, ttl=ttl,
                       bound=bound, scheduled=scheduled)


def sweep_expired() -> list:
    """Best-effort wipe of expired token files; returns the wiped ids.

    Called by the systemd timers, by `token sweep`/`token list`, and silently
    on every CLI run as a backstop. Never raises.
    """
    wiped = []
    eph_dir = store.ephemeral_dir()
    if not eph_dir.is_dir():
        return wiped
    now = time.time()
    for p in sorted(eph_dir.glob(".env.*")):
        try:
            expiry = crypto.expiry_of(p.read_bytes())
        except (crypto.NotEncryptedError, OSError):
            continue
        if expiry and expiry < now:
            try:
                crypto.secure_wipe(p)
            except OSError:
                continue
            wiped.append(p.name.removeprefix(".env."))
    return wiped


def list_active() -> list:
    """Return [(tid, expiry), ...] for the token files currently on disk."""
    active = []
    eph_dir = store.ephemeral_dir()
    if eph_dir.is_dir():
        for p in sorted(eph_dir.glob(".env.*")):
            try:
                expiry = crypto.expiry_of(p.read_bytes())
            except (crypto.NotEncryptedError, OSError):
                continue
            active.append((p.name.removeprefix(".env."), expiry))
    return active


def revoke_token(tid: str) -> bool:
    """Kill a token's key share and wipe its file. Returns True if a token file
    existed (and was wiped), False if there was none. The share is removed
    either way."""
    path = token_path(tid)
    # kill the key share regardless
    keyring.remove(share_desc(tid), persistent=system.in_system_mode())
    if not path.exists():
        return False
    crypto.secure_wipe(path)
    return True


def load_database_url_from_token(token: str) -> str:
    tid = token_id(token)
    path = token_path(tid)
    if not path.exists():
        fail("Unknown, expired, or revoked token")

    share = keyring.read(share_desc(tid), persistent=system.in_system_mode())

    # Decrypt first: a successful decrypt authenticates the header (incl. expiry).
    try:
        text = crypto.decrypt(path.read_bytes(), token_passphrase(token, share)).decode()
    except crypto.DecryptionError:
        if share is None:
            fail("Invalid token, or its kernel key share has self-destructed "
                 "(shares expire with the token and do not survive a reboot)")
        fail("Invalid token")

    expiry = crypto.expiry_of(path.read_bytes())
    if expiry and expiry < time.time():
        crypto.secure_wipe(path)
        fail("Token expired (removed)")

    return store.url_from_env_text(text, path)


def revoke_all_tokens() -> int:
    """Remove every outstanding token file and its kernel key share.

    Tokens are self-contained encrypted URL snapshots with no env identity, so
    removing one environment can't target 'its' tokens; we revoke all of them.
    Best-effort per token so one failure doesn't strand the rest.
    """
    eph_dir = store.ephemeral_dir()
    if not eph_dir.is_dir():
        return 0
    count = 0
    for p in sorted(eph_dir.glob(".env.*")):
        tid = p.name.removeprefix(".env.")
        try:
            keyring.remove(share_desc(tid), persistent=system.in_system_mode())
        except Exception:
            pass
        try:
            crypto.secure_wipe(p)
            count += 1
        except OSError:
            pass
    return count
