"""`python -m tbmcp` — the form the daemon self-spawns with, so it works from any venv."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
