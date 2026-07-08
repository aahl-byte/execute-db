"""Shared engine for the execute-db / explore-db command-line tools.

The two front-end packages (`execute_db`, `explore_db`) are thin: each installs
an `app.AppSpec` and calls `db_core.cli.main`. All real work — the config store,
encryption, tokens, query execution, and the argparse command layer — lives here
and is parameterized by the active `AppSpec` (see `db_core.app`).
"""
