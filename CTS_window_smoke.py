"""Dry-run smoke diagnostic for Elite Dangerous window discovery.

Usage::

    python -m CTS_window_smoke --dry-run --target-fid TESTFID

Exits 0 on success (even when no live window is found — that is a valid
result in CI / mock environments).
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CTS window-discovery smoke test",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run discovery without attempting focus (always on)",
    )
    parser.add_argument(
        "--target-fid",
        default="",
        help="Carrier FID to echo in the diagnostic output",
    )
    args = parser.parse_args()

    # Late import so the module is usable without the rest of CTS on sys.path.
    from TraversalSystem.window_manager import diagnose

    report = diagnose(target_fid=args.target_fid)

    print(json.dumps(report, indent=2))
    print(f"\n{report['message']}", file=sys.stderr)

    # Always exit 0 — "no live window" is a valid state, not a failure.
    sys.exit(0)


if __name__ == "__main__":
    main()
