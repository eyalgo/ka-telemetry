"""`python3 -m ka_telemetry` — runs the same command as the installed `ka-telemetry`.

It adds no parser and no dispatch of its own: every argument list goes to `ka_telemetry.cli.main`,
the target `pyproject.toml` names for the console entry, so the two ways in print the same thing
and exit with the same status.
"""

from __future__ import annotations

import sys

from ka_telemetry.cli import main

if __name__ == "__main__":
    sys.exit(main())
