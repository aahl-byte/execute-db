"""Ephemeral tokens: short-lived, password-free access to one environment.

A token is a copy of an environment's credentials re-encrypted under a fresh
random secret with the expiry sealed into the authenticated header. Half of the
key (a key share) lives only in the kernel keyring with a TTL, so at expiry (or
reboot) the share self-destructs and the token file becomes undecryptable.
"""

import hashlib
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import crypto, kernel_keyring, paths, system
from .envs import read_env_text, url_from_env_text, write_encrypted
from .paths import env_file_path
from .util import fail

TTL_RE = re.compile(r"^(\d+)([smhd])$")
TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

TOKEN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


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
    return paths.EPHEMERAL_DIR / f".env.{tid}"


def share_desc(tid: str) -> str:
    return f"execute-db:token:{tid}"


def token_passphrase(token: str, share: bytes) -> str:
    return f"{token}:{share.decode()}" if share else token


def cli_binary():
    """Absolute path to the execute-db entry point, for systemd units."""
    candidate = Path(sys.executable).with_name("execute-db")
    if candidate.exists():
        return str(candidate)
    return shutil.which("execute-db")


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
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service = unit_dir / "execute-db-token-sweep.service"
    timer = unit_dir / "execute-db-token-sweep.timer"
    if service.exists() and timer.exists():
        return
    unit_dir.mkdir(parents=True, exist_ok=True)
    service.write_text(
        "[Unit]\n"
        "Description=Wipe expired execute-db ephemeral tokens\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exe} token sweep\n"
    )
    timer.write_text(
        "[Unit]\n"
        "Description=Wipe expired execute-db ephemeral tokens after startup\n\n"
        "[Timer]\n"
        "OnStartupSec=2min\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    for args in (["daemon-reload"], ["enable", "--now", timer.name]):
        subprocess.run(["systemctl", "--user", *args], capture_output=True)


def cmd_token_create(env: str, ttl: str):
    ttl_seconds = parse_ttl(ttl)

    # Decrypt (or read) the source env; this is where the password gate applies.
    path = env_file_path(env)
    if not path.exists():
        fail(f"Env file not found: {path}")
    text = read_env_text(env, path)

    token = secrets.token_urlsafe(16)
    tid = token_id(token)
    expiry = int(time.time()) + ttl_seconds

    # Bind the file to a key share that lives only in the kernel keyring with a
    # TTL: at expiry (or reboot) the kernel destroys the share, and no copy of
    # the file can ever be decrypted again — even by someone holding the token.
    share = secrets.token_hex(32).encode()
    bound = kernel_keyring.store(share_desc(tid), share, ttl_seconds + 2,
                                 persistent=system.in_system_mode())
    passphrase = token_passphrase(token, share if bound else None)

    paths.EPHEMERAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_encrypted(token_path(tid), crypto.encrypt(text.encode(), passphrase, expiry))

    install_boot_sweep()
    scheduled = schedule_token_wipe(ttl_seconds)

    print(f"Token: {token}")
    print(f"  id:      {tid}")
    print(f"  env:     {env}")
    print(f"  expires: {datetime.fromtimestamp(expiry):%Y-%m-%d %H:%M:%S} ({ttl})")
    if bound:
        print("  key share: in kernel keyring, self-destructs at expiry "
              "(token will not survive a reboot)")
    else:
        print("  key share: UNAVAILABLE (no kernel keyring) — a copied token "
              "file stays decryptable with the token after expiry",
              file=sys.stderr)
    if scheduled:
        print("  auto-wipe: systemd user timer scheduled at expiry")
    else:
        print("  auto-wipe: could not schedule a systemd user timer — the file "
              "will only be wiped on the next execute-db run after expiry",
              file=sys.stderr)
    print(f'Use it with: execute-db --token {token} "SELECT ..."')
    print("This token is shown once and cannot be recovered.")


def sweep_expired_tokens(verbose: bool = False) -> list:
    """Best-effort wipe of expired token files; returns the wiped ids.

    Called by the systemd timers, by `token sweep`/`token list`, and silently
    on every CLI run as a backstop. Never raises.
    """
    wiped = []
    if not paths.EPHEMERAL_DIR.is_dir():
        return wiped
    now = time.time()
    for p in sorted(paths.EPHEMERAL_DIR.glob(".env.*")):
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
    if verbose:
        for tid in wiped:
            print(f"wiped expired token {tid}", file=sys.stderr)
    return wiped


def cmd_token_list():
    sweep_expired_tokens(verbose=True)
    active = []
    if paths.EPHEMERAL_DIR.is_dir():
        for p in sorted(paths.EPHEMERAL_DIR.glob(".env.*")):
            try:
                expiry = crypto.expiry_of(p.read_bytes())
            except (crypto.NotEncryptedError, OSError):
                continue
            active.append((p.name.removeprefix(".env."), expiry))
    if not active:
        print("No active tokens.")
        return
    for tid, expiry in active:
        print(f"{tid}  expires {datetime.fromtimestamp(expiry):%Y-%m-%d %H:%M:%S}")


def cmd_token_revoke(tid: str):
    path = token_path(tid)
    # kill the key share regardless
    kernel_keyring.remove(share_desc(tid), persistent=system.in_system_mode())
    if not path.exists():
        fail(f"No token with id '{tid}' (see `execute-db token list`)")
    crypto.secure_wipe(path)
    print(f"Revoked token {tid}")


def load_database_url_from_token(token: str) -> str:
    tid = token_id(token)
    path = token_path(tid)
    if not path.exists():
        fail("Unknown, expired, or revoked token")

    share = kernel_keyring.read(share_desc(tid), persistent=system.in_system_mode())

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

    return url_from_env_text(text, path)


def revoke_all_tokens() -> int:
    """Remove every outstanding token file and its kernel key share.

    Tokens are self-contained encrypted URL snapshots with no env identity, so
    removing one environment can't target 'its' tokens; we revoke all of them.
    Best-effort per token so one failure doesn't strand the rest.
    """
    if not paths.EPHEMERAL_DIR.is_dir():
        return 0
    count = 0
    for p in sorted(paths.EPHEMERAL_DIR.glob(".env.*")):
        tid = p.name.removeprefix(".env.")
        try:
            kernel_keyring.remove(share_desc(tid), persistent=system.in_system_mode())
        except Exception:
            pass
        try:
            crypto.secure_wipe(p)
            count += 1
        except OSError:
            pass
    return count
