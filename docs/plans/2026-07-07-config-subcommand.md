# `execute-db config` Subcommand Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Add a first-class `execute-db config` command (`list` / `set` / `rm`) that manages environments in place through the trusted path, and delete `config.json` entirely — environments become simply the `.env.<alias>` files present in the store.

**Architecture:** Environments are discovered by globbing `.env.<alias>` files in `CONFIG_DIR` instead of reading a `config.json` index. Direct-URL environments are removed (every env is a file, and hardened mode already required that). `config set <alias>` prompts for the URL on `/dev/tty` (never argv — argv leaks through sudo logs, `/proc/<pid>/cmdline`, and shell history), prompts for a new password, and writes an atomically-replaced encrypted file. `config rm <alias>` wipes the file and revokes all outstanding tokens. The command runs as the `executedb` service user in hardened mode via the existing launcher redirect, exactly like `password`/`token`.

**Tech Stack:** Python 3.9+, `argparse`, `cryptography` (existing `execute_db.crypto`), `pytest` 9.x. No new dependencies.

---

## Locked design decisions (from design discussion)

1. **`config.json` is deleted.** Env list is derived from `.env.<alias>` files. Direct-URL envs are dropped.
2. **`config set` = full replace + new password** every time. It creates-or-replaces, always re-prompts for a URL and a new password, always writes fresh ciphertext. This deliberately triples as create / edit-URL / reset-forgotten-password.
3. **`config rm` revokes tokens.** Because a token file is a self-contained encrypted URL snapshot carrying no env identity (and `rm` can't decrypt it), `rm` revokes **all** outstanding tokens rather than adding a cleartext `tid→alias` index. This is belt-and-suspenders; the real cutoff for a removed env is server-side password rotation (already in the threat model).
4. **Works in both plain and hardened installs.** The only difference is which store the tty-prompted write lands in — handled already by `CONFIG_DIR` resolution + the launcher redirect.
5. **No migration machinery.** Conventional `.env.<alias>` files are discovered automatically. A leftover `config.json` triggers a one-line stderr notice and is left untouched (never auto-deleted — it may hold a direct URL the user still needs to copy).

---

## Reference: current code shape

- `execute_db/cli.py`
  - `CONFIG_DIR` (module global, resolved from uid in system mode), `CONFIG_FILE = CONFIG_DIR/"config.json"`, `EPHEMERAL_DIR = CONFIG_DIR/".ephemeral"`.
  - `ENV_NAME_RE = ^[A-Za-z][A-Za-z0-9_-]*$`, `RESERVED_NAMES = {"password","token","file","f","help","sql"}`.
  - `config_environments(config)`, `init_config()`, `load_config()`, `env_file_path(config, env)`, `load_database_url(config, env)`, `env_flag_help(config, env)`, `add_env_flags(parser, envs, config, ...)`.
  - Callers to update: `manage_main()` (lines ~520-655), `exec_main()` (lines ~660-702), `cmd_password_set/change`, `cmd_token_create`.
  - `main()` routes `password`/`token` → `manage_main`, else → `exec_main`.
- `execute_db/crypto.py`
  - `prompt_password(prompt, confirm=False)` — reads via `getpass` after asserting `/dev/tty` opens; raises `NoTTYError`.
  - `encrypt(plaintext: bytes, password: str, expiry=0) -> bytes`, `decrypt`, `is_encrypted(path)`, `secure_wipe(path)`.
- `execute_db/kernel_keyring.py` — `store`, `read`, `remove(desc, persistent=...)`.
- `share_desc(tid)` and `token_path(tid)` helpers already exist in `cli.py`.

---

## Task 0: Bootstrap the test harness

There is currently **no `tests/` directory**. Create it and a fixture that redirects the module-global store paths to a temp dir so tests never touch the real `~/.execute-db`.

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Create: `tests/test_discovery.py` (used from Task 1 on)
- Modify: `pyproject.toml` (add a `[dependency-groups]`/optional test dep + pytest config)

**Step 1: Add pytest config and dev dependency**

Append to `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**Step 2: Write `tests/conftest.py`**

`CONFIG_DIR`, `CONFIG_FILE`, and `EPHEMERAL_DIR` are module globals looked up at call time, so monkeypatching the module attributes is sufficient. Functions that read `CONFIG_FILE`/`EPHEMERAL_DIR` derived from `CONFIG_DIR` must be repointed too.

```python
import pytest
from execute_db import cli


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the CLI's store globals at a temp dir and return it."""
    d = tmp_path / ".execute-db"
    d.mkdir()
    monkeypatch.setattr(cli, "CONFIG_DIR", d)
    monkeypatch.setattr(cli, "CONFIG_FILE", d / "config.json")
    monkeypatch.setattr(cli, "EPHEMERAL_DIR", d / ".ephemeral")
    # Never let a test think it is running as the service user.
    monkeypatch.setattr(cli, "in_system_mode", lambda: False)
    return d
```

**Step 3: Verify the harness imports**

Run: `python3 -m pytest tests/ -q`
Expected: `no tests ran` (collection succeeds, 0 tests). If import fails, fix before proceeding.

**Step 4: Commit**

```bash
git add tests/ pyproject.toml
git commit -m "test: bootstrap pytest harness with temp-store fixture"
```

---

## Task 1: Environment discovery (replace `config.json`)

Introduce `discover_envs()` and delete the `config.json` reader.

**Files:**
- Modify: `execute_db/cli.py` (`RESERVED_NAMES`, add `discover_envs`, remove `config_environments`, `load_config`)
- Test: `tests/test_discovery.py`

**Step 1: Write the failing test**

```python
from execute_db import cli


def _write(store, name, body=b"DATABASE_URL=postgresql://x\n"):
    (store / name).write_bytes(body)


def test_discovers_env_files_as_aliases(store):
    _write(store, ".env.dev")
    _write(store, ".env.staging")
    assert cli.discover_envs() == ["dev", "staging"]


def test_ignores_non_env_and_temp_and_reserved(store):
    _write(store, ".env.dev")
    _write(store, ".env.dev.tmp")          # in-progress write
    _write(store, "config.json", b"{}")     # legacy index
    _write(store, ".env.token")             # reserved name
    (store / ".ephemeral").mkdir()          # token dir, not an env
    _write(store / ".ephemeral", ".env.abc123def456") if False else None
    assert cli.discover_envs() == ["dev"]


def test_empty_store_returns_empty(store):
    assert cli.discover_envs() == []
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_discovery.py -q`
Expected: FAIL — `AttributeError: module 'execute_db.cli' has no attribute 'discover_envs'`.

**Step 3: Implement**

Add `"config"` to `RESERVED_NAMES` and add the discovery function. Place `discover_envs` where `config_environments`/`load_config` were.

```python
RESERVED_NAMES = {"password", "token", "config", "file", "f", "help", "sql"}


def discover_envs() -> list:
    """Environments are the `.env.<alias>` files in CONFIG_DIR (no config.json).

    The alias is the filename suffix. Files may be plaintext (plain install) or
    encrypted; `.tmp` writes, the `.ephemeral` token dir, and any leftover
    `config.json` are ignored.
    """
    if not CONFIG_DIR.is_dir():
        return []
    envs = []
    for p in sorted(CONFIG_DIR.glob(".env.*")):
        if p.name.endswith(".tmp") or not p.is_file():
            continue
        alias = p.name[len(".env."):]
        if alias in RESERVED_NAMES or not ENV_NAME_RE.match(alias):
            print(f"Ignoring invalid environment file {p.name} in {CONFIG_DIR}",
                  file=sys.stderr)
            continue
        envs.append(alias)
    if CONFIG_FILE.exists():
        print(f"Note: {CONFIG_FILE} is no longer used; environments are read from "
              f".env.* files. A direct-URL env must be recreated with "
              f"`execute-db config set <name>`.", file=sys.stderr)
    return envs
```

Delete `config_environments()` and `load_config()`. (`init_config` is handled in Task 8.)

**Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_discovery.py -q`
Expected: PASS (3 tests). `.env.*` glob is non-recursive so the `.ephemeral` dir is never descended.

**Step 5: Commit**

```bash
git add execute_db/cli.py tests/test_discovery.py
git commit -m "feat: discover environments from .env.* files; drop config.json reader"
```

---

## Task 2: Rewire callers; delete direct-URL support

Update every function that took a `config` dict to use the env list / fixed path convention, and remove the direct-URL branches.

**Files:**
- Modify: `execute_db/cli.py` (`env_file_path`, `load_database_url`, `env_flag_help`, `add_env_flags`, `cmd_password_set`, `cmd_password_change`, `cmd_token_create`, `manage_main`, `exec_main`)
- Test: `tests/test_discovery.py` (add `env_file_path` assertion)

**Step 1: Add a failing test for the fixed path**

```python
def test_env_file_path_is_conventional(store):
    assert cli.env_file_path("dev") == store / ".env.dev"
```

Run: `python3 -m pytest tests/test_discovery.py::test_env_file_path_is_conventional -q`
Expected: FAIL — signature is currently `env_file_path(config, env)`.

**Step 2: Simplify `env_file_path` and `load_database_url`**

```python
def env_file_path(env: str) -> Path:
    return CONFIG_DIR / f".env.{env}"


def load_database_url(env: str) -> str:
    path = env_file_path(env)
    if not path.exists():
        fail(f"Environment '{env}' not found (looked for {path}). "
             f"Create it with `execute-db config set {env}`.")
    return url_from_env_text(read_env_text(env, path), path)
```

Delete the old direct-URL branch and the `require_encrypted` call for direct URLs (`require_encrypted` stays — it still guards *plaintext files* in system mode via `read_env_text`).

**Step 3: Simplify `env_flag_help` and `add_env_flags`**

```python
def env_flag_help(env: str) -> str:
    path = env_file_path(env)
    if crypto.is_encrypted(path):
        return f"the '{env}' environment (password protected)"
    return f"the '{env}' environment (plaintext {path.name})"


def add_env_flags(parser, envs, required: bool = True):
    group = parser.add_mutually_exclusive_group(required=required)
    for env in envs:
        group.add_argument(f"--{env}", dest=env_dest(env), action="store_true",
                           help=env_flag_help(env))
    return group
```

**Step 4: Update `cmd_password_set` / `cmd_password_change` / `cmd_token_create`**

Replace `path = env_file_path(config, env)` with `path = env_file_path(env)` and delete the `if path is None:` direct-URL blocks. In `cmd_token_create` delete the `text = f"DATABASE_URL={config[env]}\n"` branch; always read the file:

```python
def cmd_token_create(env: str, ttl: str):
    ttl_seconds = parse_ttl(ttl)
    path = env_file_path(env)
    if not path.exists():
        fail(f"Env file not found: {path}")
    text = read_env_text(env, path)
    ...
```

Update the signatures `cmd_password_set(env)`, `cmd_password_change(env)`, `cmd_token_create(env, ttl)` (drop the `config` param).

**Step 5: Update `manage_main` and `exec_main`**

In both, replace:

```python
config = load_config()
envs = config_environments(config)
```

with:

```python
envs = discover_envs()
```

and update the `add_env_flags(..., config)` calls to drop `config`, and the dispatch calls:
- `cmd_password_set(env)`, `cmd_password_change(env)`, `cmd_token_create(selected_env(args, envs), args.ttl)`
- `load_database_url(env)` in `exec_main`.

Fix the two description strings that mention `config.json`:
- `exec_main` parser description line `f"back on error. Environments are the keys of {CONFIG_FILE};\n"` → `"back on error. Each environment is a .env.<name> file in the store;\n"`.

**Step 6: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS. Also smoke-check import: `python3 -c "import execute_db.cli"` → no error.

**Step 7: Commit**

```bash
git add execute_db/cli.py tests/test_discovery.py
git commit -m "refactor: thread env list instead of config dict; remove direct-URL envs"
```

---

## Task 3: TTY URL prompt helper

`config set` needs to read the connection string from the controlling terminal (never argv). Reuse the `/dev/tty` gate that `prompt_password` uses; no echo, since the URL embeds a password.

**Files:**
- Modify: `execute_db/crypto.py` (add `prompt_secret_line`)
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

```python
from execute_db import crypto


def test_prompt_secret_line_rejects_empty(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass", lambda prompt="": "")
    import pytest
    with pytest.raises(crypto.CryptoError):
        crypto.prompt_secret_line("URL: ")


def test_prompt_secret_line_returns_value(monkeypatch):
    monkeypatch.setattr(crypto, "_tty_available", lambda: True)
    monkeypatch.setattr(crypto.getpass, "getpass", lambda prompt="": "postgresql://a")
    assert crypto.prompt_secret_line("URL: ") == "postgresql://a"
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: FAIL — no `prompt_secret_line` / `_tty_available`.

**Step 3: Implement in `crypto.py`**

Refactor the `/dev/tty` check out of `prompt_password` into a helper, then add the line prompt:

```python
def _tty_available() -> bool:
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def prompt_secret_line(prompt: str) -> str:
    """Read a single non-empty secret line from the terminal (no echo).

    Used for values that embed credentials (e.g. a DATABASE_URL) so they never
    land in argv, shell history, sudo logs, or /proc/<pid>/cmdline.
    """
    if not _tty_available():
        raise NoTTYError("no interactive terminal available")
    value = getpass.getpass(prompt).strip()
    if not value:
        raise CryptoError("value must not be empty")
    return value
```

Update `prompt_password` to call `if not _tty_available(): raise NoTTYError(...)` instead of its inline `open("/dev/tty")` block (keeps one source of truth; behavior unchanged).

**Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: PASS (2 tests).

**Step 5: Commit**

```bash
git add execute_db/crypto.py tests/test_config.py
git commit -m "feat: add prompt_secret_line for tty-only URL entry"
```

---

## Task 4: `config list`

**Files:**
- Modify: `execute_db/cli.py` (add `cmd_config_list`)
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

```python
from execute_db import cli


def test_config_list_shows_state(store, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")   # plaintext
    (store / ".env.prod").write_bytes(b"EXDB1" + b"\x00" * 32)            # encrypted magic
    cli.cmd_config_list()
    out = capsys.readouterr().out
    assert "dev" in out and "plaintext" in out
    assert "prod" in out and "encrypted" in out


def test_config_list_empty(store, capsys):
    cli.cmd_config_list()
    assert "No environments" in capsys.readouterr().out
```

> Check `crypto.is_encrypted` detects the `EXDB1` magic prefix; if it needs a longer/structured header, write a real encrypted blob via `crypto.encrypt(b"DATABASE_URL=postgresql://x\n", "pw")` in the test instead of the raw magic.

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_config.py -k config_list -q`
Expected: FAIL — no `cmd_config_list`.

**Step 3: Implement**

```python
def cmd_config_list():
    envs = discover_envs()
    if not envs:
        print("No environments. Create one with `execute-db config set <name>`.")
        return
    for env in envs:
        state = "encrypted" if crypto.is_encrypted(env_file_path(env)) else "plaintext"
        print(f"{env}  ({state})")
```

**Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_config.py -k config_list -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add execute_db/cli.py tests/test_config.py
git commit -m "feat: add `config list`"
```

---

## Task 5: `config set <alias>`

Create-or-replace an environment: validate the alias, prompt for URL (tty) and a new password, encrypt in memory, and atomically write `.env.<alias>`. Never writes plaintext to disk.

**Files:**
- Modify: `execute_db/cli.py` (add `validate_alias`, `cmd_config_set`)
- Test: `tests/test_config.py`

**Step 1: Write the failing tests**

```python
import pytest
from execute_db import cli, crypto


def test_config_set_creates_encrypted_env(store, monkeypatch):
    monkeypatch.setattr(crypto, "prompt_secret_line", lambda p: "postgresql://u:p@h/db")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False: "hunter2")
    cli.cmd_config_set("dev")

    path = store / ".env.dev"
    assert path.exists()
    assert crypto.is_encrypted(path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    # round-trips back to the URL
    text = crypto.decrypt(path.read_bytes(), "hunter2").decode()
    assert "DATABASE_URL=postgresql://u:p@h/db" in text
    # no plaintext temp left behind
    assert not (store / ".env.dev.tmp").exists()


def test_config_set_replaces_existing(store, monkeypatch):
    (store / ".env.dev").write_bytes(b"old")
    monkeypatch.setattr(crypto, "prompt_secret_line", lambda p: "postgresql://new")
    monkeypatch.setattr(crypto, "prompt_password", lambda p, confirm=False: "pw")
    cli.cmd_config_set("dev")
    assert crypto.is_encrypted(store / ".env.dev")


@pytest.mark.parametrize("bad", ["token", "config", "1abc", "a b", "../x", ""])
def test_config_set_rejects_bad_alias(store, bad):
    with pytest.raises(SystemExit):
        cli.cmd_config_set(bad)
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_config.py -k config_set -q`
Expected: FAIL — no `cmd_config_set`.

**Step 3: Implement**

```python
def validate_alias(alias: str):
    if alias in RESERVED_NAMES or not ENV_NAME_RE.match(alias):
        fail(f"Invalid environment name {alias!r} "
             f"(must match {ENV_NAME_RE.pattern} and not be reserved)")


def cmd_config_set(alias: str):
    validate_alias(alias)
    path = env_file_path(alias)
    action = "Replacing" if path.exists() else "Creating"
    print(f"{action} environment '{alias}'.")

    try:
        url = crypto.prompt_secret_line(f"Connection URL for '{alias}': ")
    except crypto.NoTTYError:
        fail("A terminal is required to enter the connection URL "
             "(it must not be passed on the command line).")
    if not (url.startswith("postgresql://") or url.startswith("postgres://")):
        fail("URL must start with postgresql:// or postgres://")

    try:
        password = crypto.prompt_password(f"New password for '{alias}': ", confirm=True)
    except crypto.CryptoError as e:
        fail(str(e))

    blob = crypto.encrypt(f"DATABASE_URL={url}\n".encode(), password)
    write_encrypted(path, blob)   # temp write + chmod 600 + atomic replace
    print(f"Saved {path}")
    print("If you forget the password, run `config set` again to overwrite it.")
```

Note: `write_encrypted` already exists (temp file, `chmod 0600`, `.replace()`), so no direct-URL plaintext ever hits disk. In hardened mode the process runs as `executedb`, so the file is owned correctly.

**Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_config.py -k config_set -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add execute_db/cli.py tests/test_config.py
git commit -m "feat: add `config set` (tty-prompted URL, atomic encrypted write)"
```

---

## Task 6: `config rm <alias>`

Wipe the env file and revoke all outstanding tokens.

**Files:**
- Modify: `execute_db/cli.py` (add `cmd_config_rm`, plus a `revoke_all_tokens` helper)
- Test: `tests/test_config.py`

**Step 1: Write the failing tests**

```python
def test_config_rm_wipes_env_and_revokes_tokens(store, monkeypatch):
    (store / ".env.dev").write_bytes(b"EXDB1" + b"\x00" * 32)
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
    import pytest
    with pytest.raises(SystemExit):
        cli.cmd_config_rm("nope")
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_config.py -k config_rm -q`
Expected: FAIL — no `cmd_config_rm`.

**Step 3: Implement**

```python
def revoke_all_tokens():
    """Remove every outstanding token file and its kernel key share.

    Tokens are self-contained encrypted URL snapshots with no env identity, so
    removing one environment can't target 'its' tokens; we revoke all of them.
    Best-effort per token so one failure doesn't strand the rest.
    """
    if not EPHEMERAL_DIR.is_dir():
        return 0
    count = 0
    for p in sorted(EPHEMERAL_DIR.glob(".env.*")):
        tid = p.name.removeprefix(".env.")
        try:
            kernel_keyring.remove(share_desc(tid), persistent=in_system_mode())
        except Exception:
            pass
        try:
            crypto.secure_wipe(p)
            count += 1
        except OSError:
            pass
    return count


def cmd_config_rm(alias: str):
    validate_alias(alias)
    path = env_file_path(alias)
    if not path.exists():
        fail(f"No environment '{alias}' (see `execute-db config list`).")
    crypto.secure_wipe(path)
    revoked = revoke_all_tokens()
    print(f"Removed environment '{alias}'.")
    if revoked:
        print(f"Revoked {revoked} outstanding token(s). Rotate the database "
              f"password server-side to fully cut off access.")
```

**Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_config.py -k config_rm -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add execute_db/cli.py tests/test_config.py
git commit -m "feat: add `config rm` (wipe env, revoke outstanding tokens)"
```

---

## Task 7: Wire `config` into the CLI parser and `main()`

**Files:**
- Modify: `execute_db/cli.py` (`main()`, add `config_main()`)
- Test: `tests/test_config.py` (dispatch via `main`)

**Step 1: Write the failing test**

```python
def test_config_main_lists(store, monkeypatch, capsys):
    (store / ".env.dev").write_bytes(b"DATABASE_URL=postgresql://x\n")
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "config", "list"])
    cli.config_main()
    assert "dev" in capsys.readouterr().out
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_config.py -k config_main -q`
Expected: FAIL — no `config_main`.

**Step 3: Implement `config_main` and route it**

```python
def config_main():
    parser = argparse.ArgumentParser(
        prog="execute-db config",
        description="Manage execute-db environments (each is a .env.<name> file).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  execute-db config list\n"
               "  execute-db config set dev       # prompts for URL + password\n"
               "  execute-db config rm staging",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="{list,set,rm}")
    sub.add_parser("list", help="list configured environments")
    p_set = sub.add_parser("set", help="create or replace an environment")
    p_set.add_argument("alias", help="environment name (e.g. dev)")
    p_rm = sub.add_parser("rm", help="remove an environment and revoke tokens")
    p_rm.add_argument("alias", help="environment name to remove")

    args = parser.parse_args(sys.argv[2:])
    if args.action == "list":
        cmd_config_list()
    elif args.action == "set":
        cmd_config_set(args.alias)
    elif args.action == "rm":
        cmd_config_rm(args.alias)
```

In `main()`, add routing (before the `password`/`token` branch):

```python
def main():
    maybe_redirect_to_launcher()
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        config_main()
        return
    # ... existing sweep + password/token/exec dispatch ...
```

> Keep the existing token-sweep skip logic intact; `config` should run *after* the redirect (so it operates on the service store in hardened mode) but does not need the token sweep.

**Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all tests).

**Step 5: Commit**

```bash
git add execute_db/cli.py tests/test_config.py
git commit -m "feat: route `config` subcommand through main()"
```

---

## Task 8: First-run / empty-store onboarding

`init_config()` used to scatter dummy `.env` files and a `config.json`. Remove that; empty store now points the user at `config set`.

**Files:**
- Modify: `execute_db/cli.py` (delete `init_config`; adjust `exec_main`/`manage_main` empty-env handling)
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

```python
def test_exec_main_empty_store_guides_user(store, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["execute-db", "--dev", "SELECT 1"])
    import pytest
    with pytest.raises(SystemExit):
        cli.exec_main()
    err = capsys.readouterr().err
    assert "config set" in err
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_config.py -k empty_store -q`
Expected: FAIL — currently `init_config` runs / `--dev` may be created, or the error text differs.

**Step 3: Implement**

- Delete `init_config()`.
- In `exec_main` and `manage_main`, after `envs = discover_envs()`:

```python
    if not envs:
        fail("No environments configured. Create one with "
             "`execute-db config set <name>`.")
```

Place this guard before `add_env_flags` so argparse doesn't build an empty mutually-exclusive required group (which would otherwise error less helpfully).

**Step 4: Run the suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add execute_db/cli.py tests/test_config.py
git commit -m "feat: replace config.json auto-init with config-set onboarding"
```

---

## Task 9: Stop migrating `config.json` in `install.sh`

The hardened installer copies `config.json` into the service store. With the file gone, drop it from migration (encrypted `.env.*` files still migrate and are still required to be encrypted).

**Files:**
- Modify: `install.sh` (`_each_store_file` loop at ~line 105)

**Step 1: Edit the migration glob**

Change:

```sh
for entry in "$src"/* "$src"/.env* "$src"/config.json; do
```

to:

```sh
for entry in "$src"/* "$src"/.env*; do
```

and in the `case "$base"` skip list, add `config.json) continue ;;` so a stale index in a user's home store is explicitly not copied.

**Step 2: Lint the script**

Run: `bash -n install.sh`
Expected: no output (syntax OK). If `shellcheck` is available: `shellcheck install.sh` — no new errors.

**Step 3: Commit**

```bash
git add install.sh
git commit -m "install: stop migrating config.json (envs are .env.* files now)"
```

---

## Task 10: README — rewrite config docs

**Files:**
- Modify: `README.md` (Setup, Config format, Dynamic environments, and any `config.json`/direct-URL mentions; add a `config` command section)

**Step 1: Rewrite**

- **Setup:** remove the `config.json` table row and "edit the generated files" flow. Replace with: "Create an environment with `execute-db config set <name>` — it prompts for the connection URL and a password, then writes an encrypted `~/.execute-db/.env.<name>`."
- **Config format:** delete the `config.json` JSON example and the direct-URL option. State the convention: one encrypted `.env.<name>` file per environment; each holds `DATABASE_URL=...`.
- **Dynamic environments:** keep the idea (any `--<name>` works) but source it from files: "Every `.env.<name>` in the store becomes a `--<name>` flag. Add one with `config set`."
- **Managing config (new short section):**
  ```
  execute-db config list          # show environments and whether each is encrypted
  execute-db config set <name>    # create/replace: prompts for URL + password
  execute-db config rm <name>     # remove it and revoke outstanding tokens
  ```
  Note that `config set` never takes the URL as an argument (it would leak via shell history, `/proc`, and sudo logs) — it is always prompted on the terminal.
- **Hardened install:** update the "your encrypted envs move there" text to say config is managed in place via `config set`/`rm` (no installer re-run needed) and that direct URLs are no longer supported.
- Remove line-134 note about "Environments configured as a direct URL … can't be encrypted".

**Step 2: Proofread**

Run: `grep -n "config.json\|direct URL\|direct-URL" README.md`
Expected: no stale references remain (except, optionally, a one-line "config.json is no longer used" migration note).

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: rewrite config docs for the config subcommand; drop config.json"
```

---

## Final verification

**Step 1: Full suite + import smoke test**

```bash
python3 -m pytest tests/ -q
python3 -c "import execute_db.cli"
bash -n install.sh
```
Expected: all tests pass; no import or syntax errors.

**Step 2: Manual end-to-end (plain install, throwaway HOME)**

```bash
tmp=$(mktemp -d)
HOME=$tmp python3 -m execute_db.cli config list          # -> "No environments..."
# config set requires a tty for prompts; drive it interactively:
HOME=$tmp python3 -m execute_db.cli config set dev        # enter a postgres URL + password
HOME=$tmp python3 -m execute_db.cli config list           # -> dev (encrypted)
HOME=$tmp python3 -m execute_db.cli config rm dev         # -> removed
```
Expected: `list` reflects each change; `.env.dev` exists encrypted after `set`, gone after `rm`. (Use `@verify` skill for a rigorous pass.)

**Step 3: Final commit if anything was tidied**

```bash
git add -A && git commit -m "chore: config subcommand end-to-end verified" || true
```

---

## Notes / gotchas for the implementer

- **`CONFIG_DIR` is a module global** resolved at import from the running uid (system mode) or `Path.home()`. Tests monkeypatch `cli.CONFIG_DIR` / `cli.CONFIG_FILE` / `cli.EPHEMERAL_DIR` (see `conftest.py`). Do **not** read `$HOME` directly in new code — go through the globals.
- **Never accept the URL as argv.** This is the whole point; `config set` takes only `<alias>` positionally and prompts for the URL.
- **`write_encrypted` is the atomic primitive** (temp + `chmod 600` + `.replace()`). Reuse it; never delete-then-create.
- **`config` must work with zero existing envs** (it creates the first one) — that's why it is routed in `main()` *before* the env-flag-building path, not through `manage_main`.
- **Keep `require_encrypted`**: it still guards *plaintext files* in hardened mode inside `read_env_text`; only the direct-URL callers of it are removed.
- **`crypto.is_encrypted` magic**: confirm the `EXDB1` prefix assumption in tests against the real `crypto.is_encrypted`; if it validates more than the prefix, build fixtures with `crypto.encrypt(...)` instead of raw bytes.
