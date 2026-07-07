import sys


def fail(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)
