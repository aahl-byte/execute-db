"""explore-db front-end: the read-only CLI.

Installs the explore-db AppSpec (read-only, ~/.explore-db) and hands off to the
shared engine in db_core. It is byte-for-byte the same code as execute-db; the
only differences are the read-only flag (every query runs in a read-only
transaction, so the server rejects writes) and the separate config directory.
"""

import sys  # noqa: F401  (tests patch cli.sys.argv)

from db_core import app, cli
from . import __version__

SPEC = app.AppSpec(name="explore-db", read_only=True, version=__version__)


def main():
    app.configure(SPEC)
    cli.main()
