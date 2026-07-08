"""Shared argparse glue: mapping environments to `--<name>` flags.

Used by the exec, password, and token commands (config takes a positional
alias, so it doesn't use these).
"""

import argparse

from ..core import crypto, store


def env_dest(env: str) -> str:
    return "env_" + env.replace("-", "_")


def selected_env(args, envs: list) -> str:
    return next((e for e in envs if getattr(args, env_dest(e))), None)


def env_flag_help(env: str) -> str:
    """Describe an env flag, marking how the environment is stored."""
    path = store.env_file_path(env)
    if crypto.is_encrypted(path):
        return f"the '{env}' environment (password protected)"
    return f"the '{env}' environment (plaintext {path.name})"


def add_env_flags(parser: argparse.ArgumentParser, envs: list,
                  required: bool = True):
    group = parser.add_mutually_exclusive_group(required=required)
    for env in envs:
        group.add_argument(
            f"--{env}", dest=env_dest(env), action="store_true",
            help=env_flag_help(env),
        )
    return group
