import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

from . import crypto, kernel_keyring

# --- privilege separation (system mode) ---------------------------------------
# When installed hardened, secrets live under a dedicated service user and the
# CLI runs as that user via sudo. See install.sh and the README.
SERVICE_USER = "executedb"
LAUNCHER = "/usr/local/bin/execute-db"
MAX_SYSTEM_TTL_SECONDS = 24 * 3600  # cap on --ttl in system mode


def service_uid():
    try:
        return pwd.getpwnam(SERVICE_USER).pw_uid
    except KeyError:
        return None


def in_system_mode() -> bool:
    """True when this process IS the service user (i.e. invoked via the sudo rule)."""
    uid = service_uid()
    return uid is not None and os.geteuid() == uid


def _resolve_config_dir() -> Path:
    # In system mode derive the home from the running uid's passwd entry, NOT
    # from $HOME: an attacker who calls the sudo rule directly without -H could
    # otherwise point CONFIG_DIR (and thus config.json) at a dir they control.
    if in_system_mode():
        return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".execute-db"
    return Path.home() / ".execute-db"


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
EPHEMERAL_DIR = CONFIG_DIR / ".ephemeral"
SYSTEM_MARKER = Path.home() / ".execute-db" / "SYSTEM"  # redirect hint (user side)

DEFAULT_ENVIRONMENTS = ["dev", "staging", "production"]

# Environments are defined by config.json keys; each key becomes an --<env> flag.
ENV_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RESERVED_NAMES = {"password", "token", "file", "f", "help", "sql"}


def config_environments(config: dict) -> list:
    envs = []
    for name in config:
        if name in RESERVED_NAMES or not ENV_NAME_RE.match(name):
            print(f"Ignoring invalid environment name in {CONFIG_FILE}: {name!r}", file=sys.stderr)
            continue
        envs.append(name)
    if not envs:
        fail(f"No valid environments defined in {CONFIG_FILE}")
    return envs


def fail(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


def init_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    CONFIG_FILE.write_text(json.dumps(
        {e: f".env.{e}" for e in DEFAULT_ENVIRONMENTS}, indent=2,
    ) + "\n")

    for env in DEFAULT_ENVIRONMENTS:
        env_file = CONFIG_DIR / f".env.{env}"
        env_file.write_text(f"DATABASE_URL=postgresql://user:password@host:5432/dbname\n")

    print(f"Created default config at: {CONFIG_DIR}")
    print(f"Update your connection strings before running queries:")
    print(f"  {CONFIG_FILE}")
    for env in DEFAULT_ENVIRONMENTS:
        print(f"  {CONFIG_DIR / f'.env.{env}'}")
    sys.exit(0)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        init_config()
    return json.loads(CONFIG_FILE.read_text())


def env_file_path(config: dict, env: str):
    """Return the env's .env file path, or None if configured as a direct URL."""
    entry = config[env]
    if entry.startswith("postgresql://") or entry.startswith("postgres://"):
        return None
    return CONFIG_DIR / entry


def require_encrypted(env: str):
    """In system mode, a plaintext env would let any agent with the sudo rule
    read/mint credentials with no password gate. Refuse it."""
    if in_system_mode():
        fail(f"Environment '{env}' is not password protected; hardened (system) "
             f"mode requires encrypted environments. Encrypt it first with "
             f"`execute-db password set --{env}`.")


def read_env_text(env: str, env_path: Path) -> str:
    """Read an env file's contents, decrypting (interactively) if encrypted."""
    data = env_path.read_bytes()
    if not crypto.is_encrypted(env_path):
        require_encrypted(env)
        return data.decode()

    try:
        password = crypto.prompt_password(f"Password for '{env}': ")
    except crypto.NoTTYError:
        fail(
            f"Environment '{env}' is encrypted; run interactively or use an "
            f"ephemeral token (execute-db token create --{env} --ttl 2h)"
        )
    try:
        return crypto.decrypt(data, password).decode()
    except crypto.DecryptionError as e:
        fail(str(e))


def url_from_env_text(text: str, source) -> str:
    values = dotenv_values(stream=StringIO(text))
    url = values.get("DATABASE_URL")
    if not url:
        fail(f"DATABASE_URL not set in {source}")
    return url


def load_database_url(config: dict, env: str) -> str:
    if env not in config:
        fail(f"Environment '{env}' not found in {CONFIG_FILE}")

    env_path = env_file_path(config, env)

    # Support direct URL string or .env filename
    if env_path is None:
        require_encrypted(env)  # direct URLs have no password gate
        return config[env]

    if not env_path.exists():
        fail(f"Env file not found: {env_path}")

    return url_from_env_text(read_env_text(env, env_path), env_path)


def write_encrypted(path: Path, blob: bytes):
    """Write an encrypted blob next to `path` and atomically move it into place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    tmp.replace(path)


def cmd_password_set(config: dict, env: str):
    path = env_file_path(config, env)
    if path is None:
        fail(f"'{env}' is a direct URL in {CONFIG_FILE}; move it into a .env file to encrypt it")
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


def cmd_password_change(config: dict, env: str):
    path = env_file_path(config, env)
    if path is None:
        fail(f"'{env}' is a direct URL in {CONFIG_FILE}; nothing to change")
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


TTL_RE = re.compile(r"^(\d+)([smhd])$")
TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ttl(text: str) -> int:
    m = TTL_RE.match(text)
    if not m:
        fail(f"Invalid --ttl {text!r} (use e.g. 45s, 30m, 2h, 1d)")
    seconds = int(m.group(1)) * TTL_UNITS[m.group(2)]
    if seconds <= 0:
        fail(f"Invalid --ttl {text!r}: must be greater than zero")
    if in_system_mode() and seconds > MAX_SYSTEM_TTL_SECONDS:
        fail(f"--ttl {text!r} exceeds the {MAX_SYSTEM_TTL_SECONDS // 3600}h maximum "
             f"in hardened (system) mode")
    return seconds


TOKEN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def token_path(tid: str) -> Path:
    # tids are always derived from sha256 hexdigests; validate before building a
    # path so a user-supplied id (e.g. `token revoke ../../.env.production`)
    # cannot escape the ephemeral dir and wipe an arbitrary file.
    if not TOKEN_ID_RE.match(tid):
        fail(f"Invalid token id {tid!r}")
    return EPHEMERAL_DIR / f".env.{tid}"


def share_desc(tid: str) -> str:
    return f"execute-db:token:{tid}"


def token_passphrase(token: str, share: bytes) -> str:
    return f"{token}:{share.decode()}" if share else token


def cmd_token_create(config: dict, env: str, ttl: str):
    ttl_seconds = parse_ttl(ttl)

    # Decrypt (or read) the source env; this is where the password gate applies.
    path = env_file_path(config, env)
    if path is None:
        require_encrypted(env)  # direct URLs have no password gate
        text = f"DATABASE_URL={config[env]}\n"
    else:
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
                                 persistent=in_system_mode())
    passphrase = token_passphrase(token, share if bound else None)

    EPHEMERAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    if not EPHEMERAL_DIR.is_dir():
        return wiped
    now = time.time()
    for p in sorted(EPHEMERAL_DIR.glob(".env.*")):
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
    if in_system_mode():
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
    if in_system_mode():  # the installed system timer covers this
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


def cmd_token_list():
    sweep_expired_tokens(verbose=True)
    active = []
    if EPHEMERAL_DIR.is_dir():
        for p in sorted(EPHEMERAL_DIR.glob(".env.*")):
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
    kernel_keyring.remove(share_desc(tid), persistent=in_system_mode())
    if not path.exists():
        fail(f"No token with id '{tid}' (see `execute-db token list`)")
    crypto.secure_wipe(path)
    print(f"Revoked token {tid}")


def load_database_url_from_token(token: str) -> str:
    tid = token_id(token)
    path = token_path(tid)
    if not path.exists():
        fail("Unknown, expired, or revoked token")

    share = kernel_keyring.read(share_desc(tid), persistent=in_system_mode())

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


def run_query(database_url: str, sql: str):
    conn = psycopg2.connect(database_url, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)

            if cur.description is not None:
                # Statement returned a result set (SELECT, or ... RETURNING).
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                print(f"Columns: {columns}")
                print(f"Row count: {len(rows)}")
                result = [dict(zip(columns, row)) for row in rows]
                print(json.dumps(result, indent=2, default=str))
            elif cur.rowcount >= 0:
                # Write with no result set (INSERT/UPDATE/DELETE).
                print(f"Rows affected: {cur.rowcount}")
            else:
                # rowcount is -1 when undefined (e.g. DDL such as CREATE/ALTER).
                print("Statement executed.")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def env_flag_help(config: dict, env: str) -> str:
    """Describe an env flag, marking how the environment is stored."""
    path = env_file_path(config, env)
    if path is None:
        return f"the '{env}' environment (plaintext URL in config.json)"
    if crypto.is_encrypted(path):
        return f"the '{env}' environment (password protected)"
    return f"the '{env}' environment (plaintext {path.name})"


def add_env_flags(parser: argparse.ArgumentParser, envs: list, config: dict,
                  required: bool = True):
    group = parser.add_mutually_exclusive_group(required=required)
    for env in envs:
        group.add_argument(
            f"--{env}", dest=env_dest(env), action="store_true",
            help=env_flag_help(config, env),
        )
    return group


def env_dest(env: str) -> str:
    return "env_" + env.replace("-", "_")


def selected_env(args, envs: list) -> str:
    return next((e for e in envs if getattr(args, env_dest(e))), None)


def manage_main():
    """Handle the `password` and `token` management subcommands."""
    config = load_config()
    envs = config_environments(config)

    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="execute-db",
        description="Manage access to execute-db environments.",
        epilog='examples:\n'
               '  execute-db password set --dev\n'
               '  execute-db password change --dev\n'
               '  execute-db token create --dev --ttl 2h\n'
               '  execute-db token list\n'
               '  execute-db token revoke 8df8dbeb3696',
        formatter_class=raw,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{password,token}")

    p_password = sub.add_parser(
        "password",
        help="encrypt env files with a password / rotate passwords",
        description=(
            "Encrypt an environment's .env file so it can only be used after\n"
            "entering its password on an interactive terminal.\n\n"
            "Files are encrypted with AES-256-GCM (scrypt-derived key). There is\n"
            "no password recovery: if you forget it, delete the encrypted file,\n"
            "recreate it with your connection string, and set a password again."
        ),
        formatter_class=raw,
    )
    pw_sub = p_password.add_subparsers(dest="action", required=True, metavar="{set,change}")
    p_set = pw_sub.add_parser(
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
    add_env_flags(p_set, envs, config)
    p_change = pw_sub.add_parser(
        "change",
        help="change the password of an encrypted .env file",
        description=(
            "Rotate an environment's password: prompts for the current password,\n"
            "then a new one (twice). The decrypted contents never touch disk."
        ),
        formatter_class=raw,
    )
    add_env_flags(p_change, envs, config)

    p_token = sub.add_parser(
        "token",
        help="create/list/revoke short-lived password-free access tokens",
        description=(
            "Ephemeral tokens grant temporary, password-free access to one\n"
            "environment — e.g. handing a script or coding agent scoped access\n"
            "for an afternoon. A token works without a terminal until it expires\n"
            "or is revoked."
        ),
        formatter_class=raw,
    )
    tok_sub = p_token.add_subparsers(dest="action", required=True, metavar="{create,list,revoke}")
    p_create = tok_sub.add_parser(
        "create",
        help="create a short-lived token for an environment",
        description=(
            "Create a token for one environment. If the environment is password\n"
            "protected you are prompted for its password — the token is a copy of\n"
            "the credentials re-encrypted under a fresh random secret with the\n"
            "expiry sealed into the authenticated header.\n\n"
            "Half of the encryption key (a key share) lives only in the kernel\n"
            "keyring with a TTL: the kernel destroys it at expiry or reboot, so\n"
            "even a copied token file becomes permanently undecryptable.\n\n"
            "The token is printed ONCE and cannot be recovered; pass it to the\n"
            'holder, who runs:  execute-db --token <TOKEN> "SELECT ..."'
        ),
        formatter_class=raw,
    )
    add_env_flags(p_create, envs, config)
    p_create.add_argument("--ttl", required=True, metavar="DURATION",
                          help="token lifetime: <n>s|m|h|d, e.g. 45s, 30m, 2h, 1d")
    tok_sub.add_parser(
        "list",
        help="list active tokens (purges expired ones)",
        description=(
            "List active token ids and their expiry times. Token files that have\n"
            "already expired are deleted as a side effect. The token secrets\n"
            "themselves are never shown — they are only displayed at creation."
        ),
        formatter_class=raw,
    )
    p_revoke = tok_sub.add_parser(
        "revoke",
        help="revoke a token by id, before it expires",
        description="Delete a token so it stops working immediately.",
    )
    p_revoke.add_argument("id", help="token id, as shown by `execute-db token list`")
    tok_sub.add_parser(
        "sweep",
        help="wipe expired token files now",
        description=(
            "Wipe any expired token files. Runs automatically via systemd user\n"
            "timers (scheduled at each token's expiry, plus once after boot) and\n"
            "as a backstop on every execute-db invocation, so you rarely need to\n"
            "run it by hand."
        ),
        formatter_class=raw,
    )

    args = parser.parse_args()

    try:
        if args.command == "password":
            env = selected_env(args, envs)
            if args.action == "set":
                cmd_password_set(config, env)
            else:
                cmd_password_change(config, env)
        elif args.command == "token":
            if args.action == "create":
                cmd_token_create(config, selected_env(args, envs), args.ttl)
            elif args.action == "list":
                cmd_token_list()
            elif args.action == "sweep":
                sweep_expired_tokens(verbose=True)
            else:
                cmd_token_revoke(args.id)
    except crypto.NoTTYError:
        fail("This command needs an interactive terminal to prompt for a password.")
    except crypto.CryptoError as e:
        fail(str(e))


def exec_main():
    parser = argparse.ArgumentParser(
        prog="execute-db",
        description=(
            "Execute SQL statements against configured databases.\n\n"
            "Statements run in a single transaction: committed on success, rolled\n"
            f"back on error. Environments are the keys of {CONFIG_FILE};\n"
            "each key becomes an --<env> flag. Password-protected environments\n"
            "prompt for their password on the terminal."
        ),
        epilog='examples:\n'
               '  execute-db --dev "INSERT INTO users (name) VALUES (\'Alice\')"\n'
               '  execute-db --dev -f migration.sql\n'
               '  execute-db --dev < migration.sql\n'
               '  execute-db --token 8YOfCttjVdI5FdUfB-X6Vw "SELECT 1"\n'
               '\n'
               'management commands (details: execute-db <command> --help):\n'
               '  password set --<env>            encrypt an env file with a password\n'
               '  password change --<env>         rotate an env file\'s password\n'
               '  token create --<env> --ttl 2h   mint a short-lived password-free token\n'
               '  token list                      show active tokens\n'
               '  token revoke <id>               revoke a token early',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    config = load_config()
    envs = config_environments(config)
    group = add_env_flags(parser, envs, config)
    group.add_argument("--token", metavar="TOKEN",
                       help="connect with an ephemeral access token instead of an "
                            "environment (no password prompt; see `execute-db token --help`)")

    parser.add_argument("sql", nargs="?",
                        help="SQL statement to execute (omit to read from -f FILE or stdin)")
    parser.add_argument("-f", "--file", metavar="FILE",
                        help="read the SQL to execute from a .sql file")
    args = parser.parse_args()

    if args.token:
        database_url = load_database_url_from_token(args.token)
    else:
        env = selected_env(args, envs)
        database_url = load_database_url(config, env)

    if args.file:
        # The trusted launcher converts -f into piped stdin *as the calling
        # user*; if -f still reaches the service-user process, it would open the
        # path as the service user (a file-read primitive). Refuse it here.
        if in_system_mode():
            fail("-f/--file is not available in hardened (system) mode; "
                 "pipe the SQL via stdin instead")
        sql = Path(args.file).read_text()
    elif args.sql:
        sql = args.sql
    elif not sys.stdin.isatty():
        sql = sys.stdin.read()
    else:
        parser.error("provide SQL as an argument, via -f FILE, or pipe to stdin")

    try:
        run_query(database_url, sql)
    except Exception as e:
        # In system mode the agent sees this over sudo; psycopg2 errors can echo
        # host/user/dbname. Keep the detail for interactive user-mode debugging.
        if in_system_mode():
            fail("Query failed")
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)


def maybe_redirect_to_launcher():
    """Convenience redirect into the hardened launcher when the store has been
    migrated to system mode. UX only — NOT a security boundary: the marker
    lives in a user-writable dir and PATH can be shadowed, so real protection
    depends on the human invoking the trusted absolute path. See the README.
    """
    if in_system_mode() or os.environ.get("EXECUTE_DB_NO_SYSTEM"):
        return
    try:
        has_marker = SYSTEM_MARKER.exists()
    except OSError:
        has_marker = False
    if has_marker and os.path.exists(LAUNCHER):
        os.execv(LAUNCHER, [LAUNCHER, *sys.argv[1:]])


def main():
    maybe_redirect_to_launcher()

    # Backstop: the systemd timers do the wall-clock wiping, but sweep here too
    # in case they were unavailable. Skip for `token` commands, which sweep for
    # themselves (verbosely). Never let this break the actual command.
    if len(sys.argv) <= 1 or sys.argv[1] != "token":
        try:
            sweep_expired_tokens()
        except Exception:
            pass

    if len(sys.argv) > 1 and sys.argv[1] in ("password", "token"):
        manage_main()
    else:
        exec_main()
