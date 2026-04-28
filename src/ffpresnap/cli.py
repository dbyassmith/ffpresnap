from __future__ import annotations

import argparse
import sys
import time

from .db import Database
from .sync import run_sync


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ffpresnap-sync",
        description="Sync NFL player data into the local ffpresnap DB.",
    )
    parser.add_argument(
        "--source",
        choices=("sleeper", "ourlads"),
        default="sleeper",
        help="which source to sync from (default: sleeper)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db = Database.open()
    started = time.monotonic()
    try:
        try:
            summary = run_sync(db, source=args.source)
        except Exception as e:
            print(f"sync failed: {e}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - started
        print(
            f"synced {summary['players_written']} players in {elapsed:.1f}s "
            f"(run_id={summary['run_id']}, source={summary['source']})"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
