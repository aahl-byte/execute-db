"""execute-db front-end: the read/write CLI.

Installs the execute-db AppSpec (read/write, ~/.execute-db) and hands off to the
shared engine in db_core. All behavior lives there.
"""

import sys  # noqa: F401  (tests patch cli.sys.argv)

from db_core import app, cli
from . import __version__

SPEC = app.AppSpec(name="execute-db", read_only=False, version=__version__)


def main():
    app.configure(SPEC)
    cli.main()
