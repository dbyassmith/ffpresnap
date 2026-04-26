from __future__ import annotations

import sys
import time

from .db import Database
from .sync import run_sync


def main() -> int:
    db = Database.open()
    started = time.monotonic()
    try:
        try:
            summary = run_sync(db)
        except Exception as e:
            print(f"sync failed: {e}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - started
        print(
            f"synced {summary['players_written']} players in {elapsed:.1f}s "
            f"(run_id={summary['run_id']})"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
