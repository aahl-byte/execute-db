"""Terminal I/O helpers shared by the command layer.

This is a leaf module: it depends on nothing else in the package. `fail` is the
one sanctioned fatal-exit path (used by both layers); the prompt/redaction
helpers are used by the command layer when it needs the controlling terminal.
"""

import re
import sys


def fail(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


def redact_url(url: str) -> str:
    """Mask the password in a connection URL for safe on-screen display.

    postgresql://user:secret@host/db -> postgresql://user:****@host/db
    Only the userinfo password is touched; everything else is preserved verbatim.
    """
    return re.sub(r"(://[^:/@]+:)[^@/]*(@)", r"\1****\2", url, count=1)


def prompt_confirm(question: str) -> bool:
    """Ask a yes/no question, reading the answer from the controlling terminal."""
    print(question, end="", flush=True)
    try:
        with open("/dev/tty") as tty:
            answer = tty.readline()
    except OSError:
        return False
    return answer.strip().lower() in ("y", "yes")
