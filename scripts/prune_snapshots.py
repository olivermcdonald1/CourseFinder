"""
Drop seat snapshots older than the retention window.

Measured: seat_daily is 186 bytes a row and gains ~14k rows a day once both
terms are sweeping, so it grows about 2.6 MB/day -- past a free 512MB Postgres
inside a year. 90 days settles it around 235 MB and still answers every
question the UI asks, since the trend line only ever spans a term.

    python scripts/prune_snapshots.py [--days 90] [--dry-run]
"""

import argparse
import os

from sqlalchemy import create_engine, text

DEFAULT_DAYS = 90


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    cutoff = text("current_date - CAST(:days AS integer)")

    with engine.begin() as conn:
        doomed = conn.execute(
            text(f"SELECT count(*) FROM seat_daily WHERE day < {cutoff.text}"),
            {"days": args.days}).scalar()
        total = conn.execute(text("SELECT count(*) FROM seat_daily")).scalar()
        print(f"seat_daily: {total} rows, {doomed} older than {args.days} days")

        if args.dry_run:
            print("dry run -- nothing deleted")
            return
        if not doomed:
            print("nothing to prune")
            return

        conn.execute(
            text(f"DELETE FROM seat_daily WHERE day < {cutoff.text}"),
            {"days": args.days})
        print(f"deleted {doomed} rows")


if __name__ == "__main__":
    main()
