"""Windowed entry point used by the Windows installer."""

from __future__ import annotations

import sys

from railway_sim.app import main as app_main


def main(argv: list[str] | None = None) -> int:
    """Start the wx interface by default while retaining diagnostic flags."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    return app_main(arguments or ["--ui", "wx"])


if __name__ == "__main__":
    raise SystemExit(main())
