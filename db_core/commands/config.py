"""The `config` command: create, list, and remove environments.

`config` manages the store in place and must work with zero environments
configured, so cli.main dispatches it before the env-flag-building paths.
"""

import argparse
import sys

from .. import app, console
from ..console import fail
from ..core import crypto, store, tokens


def cmd_list():
    envs = store.discover_envs()
    if not envs:
        print(f"No environments. Create one with `{app.current().name} config set <name>`.")
        return
    for env in envs:
        state = "encrypted" if crypto.is_encrypted(store.env_file_path(env)) else "plaintext"
        print(f"{env}  ({state})")


def read_connection_url(alias: str) -> str:
    """Prompt (hidden) for a Postgres URL, echo a password-redacted preview, and
    confirm it before use. Re-prompts on an empty/invalid URL or a declined
    preview, so a mis-paste can be retried without exposing the credential."""
    while True:
        try:
            url = crypto.prompt_line(f"Connection URL for '{alias}': ")
        except crypto.NoTTYError:
            fail("A terminal is required to enter the connection URL "
                 "(it must not be passed on the command line).")
        except crypto.CryptoError as e:
            print(str(e), file=sys.stderr)
            continue
        if not (url.startswith("postgresql://") or url.startswith("postgres://")):
            print("URL must start with postgresql:// or postgres://", file=sys.stderr)
            continue
        print(f"Read: {console.redact_url(url)}")
        if console.prompt_confirm("Looks right? [y/N] "):
            return url


def cmd_set(alias: str):
    store.validate_alias(alias)
    # First run: the store dir may not exist yet. Create it 0700 before writing.
    store.config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    path = store.env_file_path(alias)
    action = "Replacing" if path.exists() else "Creating"
    print(f"{action} environment '{alias}'.")

    url = read_connection_url(alias)

    # A password is optional: leave it blank to store the environment in
    # plaintext (both apps support this identically). In hardened (system) mode
    # a plaintext env is later refused at read time; see store.require_encrypted.
    try:
        password = crypto.prompt_password(
            f"Password for '{alias}' (leave blank for no encryption): ",
            confirm=True, allow_empty=True)
    except crypto.CryptoError as e:
        fail(str(e))

    contents = f"DATABASE_URL={url}\n"
    if password:
        store.write_encrypted(path, crypto.encrypt(contents.encode(), password))
        print(f"Saved {path} (encrypted)")
        print("If you forget the password, run `config set` again to overwrite it.")
    else:
        store.write_plaintext(path, contents)
        print(f"Saved {path} (plaintext)")
        print(f"Add a password later with `{app.current().name} password set --{alias}`.")


def cmd_rm(alias: str):
    store.validate_alias(alias)
    path = store.env_file_path(alias)
    if not path.exists():
        fail(f"No environment '{alias}' (see `{app.current().name} config list`).")
    crypto.secure_wipe(path)
    revoked = tokens.revoke_all_tokens()
    print(f"Removed environment '{alias}'.")
    if revoked:
        print(f"Revoked {revoked} outstanding token(s). Rotate the database "
              f"password server-side to fully cut off access.")


def build_parser() -> argparse.ArgumentParser:
    name = app.current().name
    parser = argparse.ArgumentParser(
        prog=f"{name} config",
        description=f"Create, list, and remove the environments {name} connects to.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               f"  {name} config list          # show what's configured\n"
               f"  {name} config set dev       # add 'dev' (prompts for URL + optional password)\n"
               f"  {name} config rm staging    # delete 'staging'",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="{list,set,rm}")
    sub.add_parser("list", help="show configured environments and whether each is encrypted")
    p_set = sub.add_parser(
        "set",
        help="add or replace an environment (prompts for connection URL + optional password)",
        description=(
            "Create or replace an environment. Prompts for a PostgreSQL URL and an\n"
            "optional password to encrypt it with (leave the password blank to store\n"
            "it in plaintext). The URL is never read from the command line.\n"
            "Re-running `set` is also how you reset a forgotten password."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_set.add_argument("alias", help="name for the environment, e.g. dev, staging, prod")
    p_rm = sub.add_parser(
        "rm",
        help="delete an environment and revoke its outstanding tokens",
        description=(
            "Securely wipe an environment's file and revoke any ephemeral tokens.\n"
            "Rotate the database password server-side afterwards to fully cut off\n"
            "any token already copied elsewhere."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_rm.add_argument("alias", help="name of the environment to remove")
    return parser


def run(argv: list):
    args = build_parser().parse_args(argv)
    try:
        if args.action == "list":
            cmd_list()
        elif args.action == "set":
            cmd_set(args.alias)
        elif args.action == "rm":
            cmd_rm(args.alias)
    except crypto.NoTTYError:
        fail("This command needs an interactive terminal.")
    except crypto.CryptoError as e:
        fail(str(e))
