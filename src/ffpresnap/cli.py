from __future__ import annotations

import argparse
import sys
import time

from .db import Database
from .feeds import adapter_names
from .sync import run_sync


def _source_choices() -> tuple[str, ...]:
    """Build the dynamic ``--source`` enum.

    Player-data sources (sleeper, ourlads) plus every registered feed
    adapter. Importing :mod:`ffpresnap.feeds` triggers adapter registration.
    """
    return ("sleeper", "ourlads") + adapter_names()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ffpresnap-sync",
        description="Sync NFL player data and feeds into the local ffpresnap DB.",
    )
    parser.add_argument(
        "--source",
        choices=_source_choices(),
        default="sleeper",
        help="which source to sync from (default: sleeper)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "feed sources only: walk the entire feed instead of stopping at "
            "the first fully-seen page (use for first-run backfill or "
            "reconciliation against API drift)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db = Database.open()
    started = time.monotonic()
    try:
        try:
            summary = run_sync(db, source=args.source, full=args.full)
        except Exception as e:
            print(f"sync failed: {e}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - started
        if "items_fetched" in summary:
            print(
                f"synced {summary['items_new']} new feed items "
                f"(matched={summary['items_matched']}, "
                f"unmatched={summary['items_unmatched']}, "
                f"fetched={summary['items_fetched']}) "
                f"in {elapsed:.1f}s "
                f"(run_id={summary['run_id']}, source={summary['source']})"
            )
        else:
            print(
                f"synced {summary['players_written']} players in {elapsed:.1f}s "
                f"(run_id={summary['run_id']}, source={summary['source']})"
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
