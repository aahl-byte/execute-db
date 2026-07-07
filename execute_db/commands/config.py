"""The `config` command: create, list, and remove environments.

`config` manages the store in place and must work with zero environments
configured, so cli.main dispatches it before the env-flag-building paths.
"""

import argparse
import sys

from .. import console
from ..console import fail
from ..core import crypto, store, tokens


def cmd_list():
    envs = store.discover_envs()
    if not envs:
        print("No environments. Create one with `execute-db config set <name>`.")
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
    store.CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = store.env_file_path(alias)
    action = "Replacing" if path.exists() else "Creating"
    print(f"{action} environment '{alias}'.")

    url = read_connection_url(alias)

    try:
        password = crypto.prompt_password(f"New password for '{alias}': ", confirm=True)
    except crypto.CryptoError as e:
        fail(str(e))

    blob = crypto.encrypt(f"DATABASE_URL={url}\n".encode(), password)
    store.write_encrypted(path, blob)   # temp write + chmod 600 + atomic replace
    print(f"Saved {path}")
    print("If you forget the password, run `config set` again to overwrite it.")


def cmd_rm(alias: str):
    store.validate_alias(alias)
    path = store.env_file_path(alias)
    if not path.exists():
        fail(f"No environment '{alias}' (see `execute-db config list`).")
    crypto.secure_wipe(path)
    revoked = tokens.revoke_all_tokens()
    print(f"Removed environment '{alias}'.")
    if revoked:
        print(f"Revoked {revoked} outstanding token(s). Rotate the database "
              f"password server-side to fully cut off access.")


def build_parser() -> argparse.ArgumentParser:
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
