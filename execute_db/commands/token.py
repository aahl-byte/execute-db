"""The `token` command: create/list/revoke/sweep ephemeral access tokens.

The handlers are presentation only — the minting, sweeping, and revoking logic
lives in `core.tokens`; here we format its results for the terminal.
"""

import argparse
import sys
from datetime import datetime

from .flags import add_env_flags, selected_env
from ..console import fail
from ..core import crypto, tokens
from ..core.store import discover_envs


def cmd_create(env: str, ttl: str):
    res = tokens.create_token(env, ttl)
    print(f"Token: {res.token}")
    print(f"  id:      {res.tid}")
    print(f"  env:     {res.env}")
    print(f"  expires: {datetime.fromtimestamp(res.expiry):%Y-%m-%d %H:%M:%S} ({res.ttl})")
    if res.bound:
        print("  key share: in kernel keyring, self-destructs at expiry "
              "(token will not survive a reboot)")
    else:
        print("  key share: UNAVAILABLE (no kernel keyring) — a copied token "
              "file stays decryptable with the token after expiry",
              file=sys.stderr)
    if res.scheduled:
        print("  auto-wipe: systemd user timer scheduled at expiry")
    else:
        print("  auto-wipe: could not schedule a systemd user timer — the file "
              "will only be wiped on the next execute-db run after expiry",
              file=sys.stderr)
    print(f'Use it with: execute-db --token {res.token} "SELECT ..."')
    print("This token is shown once and cannot be recovered.")


def _report_wiped(wiped: list):
    for tid in wiped:
        print(f"wiped expired token {tid}", file=sys.stderr)


def cmd_list():
    _report_wiped(tokens.sweep_expired())
    active = tokens.list_active()
    if not active:
        print("No active tokens.")
        return
    for tid, expiry in active:
        print(f"{tid}  expires {datetime.fromtimestamp(expiry):%Y-%m-%d %H:%M:%S}")


def cmd_revoke(tid: str):
    if not tokens.revoke_token(tid):
        fail(f"No token with id '{tid}' (see `execute-db token list`)")
    print(f"Revoked token {tid}")


def cmd_sweep():
    _report_wiped(tokens.sweep_expired())


def build_parser(envs: list) -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="execute-db token",
        description=(
            "Ephemeral tokens grant temporary, password-free access to one\n"
            "environment — e.g. handing a script or coding agent scoped access\n"
            "for an afternoon. A token works without a terminal until it expires\n"
            "or is revoked."
        ),
        formatter_class=raw,
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="{create,list,revoke}")
    p_create = sub.add_parser(
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
    add_env_flags(p_create, envs)
    p_create.add_argument("--ttl", required=True, metavar="DURATION",
                          help="token lifetime: <n>s|m|h|d, e.g. 45s, 30m, 2h, 1d")
    sub.add_parser(
        "list",
        help="list active tokens (purges expired ones)",
        description=(
            "List active token ids and their expiry times. Token files that have\n"
            "already expired are deleted as a side effect. The token secrets\n"
            "themselves are never shown — they are only displayed at creation."
        ),
        formatter_class=raw,
    )
    p_revoke = sub.add_parser(
        "revoke",
        help="revoke a token by id, before it expires",
        description="Delete a token so it stops working immediately.",
    )
    p_revoke.add_argument("id", help="token id, as shown by `execute-db token list`")
    sub.add_parser(
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
    return parser


def run(argv: list):
    envs = discover_envs()
    if not envs:
        fail("No environments configured. Create one with "
             "`execute-db config set <name>`.")
    args = build_parser(envs).parse_args(argv)
    try:
        if args.action == "create":
            cmd_create(selected_env(args, envs), args.ttl)
        elif args.action == "list":
            cmd_list()
        elif args.action == "sweep":
            cmd_sweep()
        else:
            cmd_revoke(args.id)
    except crypto.NoTTYError:
        fail("This command needs an interactive terminal to prompt for a password.")
    except crypto.CryptoError as e:
        fail(str(e))
