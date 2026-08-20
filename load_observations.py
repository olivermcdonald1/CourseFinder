#!/usr/bin/env python3
"""
Load a VSB sweep from raw XML straight into current_seats and seat_daily.

NO JSONL INTERMEDIATE
  The parser's JSONL was a useful stepping stone but doesn't survive a
  full-university sweep: 7.1MB per run, appended, with a whole-file re-read on
  every run to dedupe. At a 20-minute cadence that's ~500MB/day of files whose
  contents are already in Postgres, 150x the size of the database they feed.
  This reads the XML, transforms in memory, and writes both tables.

  parse_vsb.py still works and is still the right tool for eyeballing a sweep
  (`--stdout`); it just isn't in the load path any more.

TWO GRAINS, ONE PASS
  current_seats  UPSERT on (crn, term)       -- flat ~7.5k rows forever
  seat_daily     UPSERT on (crn, term, day)  -- one row per section per day

  Sweep frequency and snapshot frequency are decoupled: run this every 20
  minutes and current_seats stays fresh while seat_daily still gains only one
  row per section per day.

NEVER GOES BACKWARDS
  Both upserts refuse to overwrite a row with an OLDER observation
  (`WHERE excluded.observed_at >= <table>.observed_at`). Re-running against a
  stale raw directory is then a no-op rather than a corruption.

UNKNOWN SECTIONS ARE DROPPED
  A (crn, term) with no row in `sections` is skipped and counted -- the
  catalogue is the system of record for what exists. A rising count means the
  catalogue is stale, so it's reported loudly.

USAGE
  python3 load_observations.py                     # every term dir under data/raw
  python3 load_observations.py --term 202609       # one term
  python3 load_observations.py path/to/batch.xml   # specific files
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from app.db import engine
from app.models import CurrentSeats, SeatDaily, Section

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "data" / "raw"
BATCH_SIZE = 1000

# VSB reports server_time in UTC. Bucketing days on UTC midnight would split a
# Montreal evening across two "days" (UTC midnight is 8pm EDT), so days are
# local to the campus the data describes.
LOCAL_TZ = ZoneInfo("America/Toronto")

# collector/ is a scripts directory, not an installed package. Reuse its parser
# rather than maintaining a second copy of the XML walk.
sys.path.insert(0, str(HERE / "collector"))
from parse_vsb import parse_class_data  # noqa: E402

Session = sessionmaker(engine)


def xml_paths(targets, term=None):
    """Arguments -> concrete XML paths. Accepts files, dirs, or nothing."""
    if targets:
        paths = []
        for t in targets:
            p = Path(t)
            paths.extend(sorted(p.rglob("*.xml")) if p.is_dir() else [p])
        return paths

    root = RAW_DIR / term if term else RAW_DIR
    return sorted(root.rglob("*.xml"))


def read_sweep(paths):
    """Every section record across the given XML files."""
    records = []
    for path in paths:
        try:
            records.extend(parse_class_data(path.read_text(), source_file=path.name))
        except ET.ParseError as e:
            # One torn file shouldn't cost the whole sweep.
            print(f"  skip {path.name}: {e}", file=sys.stderr)
    return records


def split_rows(records, known_sections):
    """
    Records -> (current_rows, daily_rows, dropped).

    Collapses duplicates as it goes: the same section can appear in two batch
    files, and VSB repeats a block once per schedule permutation. Latest
    observation wins in both tables.
    """
    current, daily, dropped = {}, {}, {}

    for r in records:
        key = (r["crn"], r["term"])
        if key not in known_sections:
            dropped[r["course_key"]] = dropped.get(r["course_key"], 0) + 1
            continue

        observed_at = datetime.fromisoformat(r["server_time"])
        day = observed_at.astimezone(LOCAL_TZ).date()

        seen = current.get(key)
        if seen is None or observed_at >= seen["observed_at"]:
            current[key] = {
                "crn": r["crn"], "term": r["term"], "observed_at": observed_at,
                "open_seats": r["open_seats"],
                "waitlist_count": r["waitlist_count"],
                "waitlist_seats": r["waitlist_seats"],
                "is_full": r["is_full"], "max_enrol": r["max_enrol"],
                "non_reserved": r["non_reserved"], "status": r["status"],
            }

        dkey = (r["crn"], r["term"], day)
        seen = daily.get(dkey)
        if seen is None or observed_at >= seen["observed_at"]:
            daily[dkey] = {
                "crn": r["crn"], "term": r["term"], "day": day,
                "observed_at": observed_at,
                "open_seats": r["open_seats"],
                "waitlist_count": r["waitlist_count"],
                "waitlist_seats": r["waitlist_seats"],
                "is_full": r["is_full"], "max_enrol": r["max_enrol"],
            }

    return list(current.values()), list(daily.values()), dropped


def upsert_latest(session, model, rows, key_columns):
    """
    UPSERT, but only when the incoming observation is at least as new.

    The WHERE clause on DO UPDATE is what makes re-running against an old raw
    directory harmless -- without it, replaying yesterday's XML would stamp
    yesterday's seat counts onto today's row.
    """
    if not rows:
        return 0

    updatable = [c for c in rows[0] if c not in key_columns]
    table = model.__table__

    for start in range(0, len(rows), BATCH_SIZE):
        stmt = insert(model).values(rows[start:start + BATCH_SIZE])
        stmt = stmt.on_conflict_do_update(
            index_elements=key_columns,
            set_={c: stmt.excluded[c] for c in updatable},
            where=(stmt.excluded.observed_at >= table.c.observed_at),
        )
        session.execute(stmt)

    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*", help="XML files or directories")
    ap.add_argument("--term", help="only this term's raw directory")
    args = ap.parse_args()

    paths = xml_paths(args.targets, args.term)
    if not paths:
        print("no XML found -- run vsb_collect.py first", file=sys.stderr)
        return 1

    records = read_sweep(paths)
    if not records:
        print("no sections parsed from those files", file=sys.stderr)
        return 1

    with Session() as session:
        # One round trip for the whole membership test. ~15k pairs is nothing
        # to hold in memory, and the alternative is a query per section.
        known = set(session.execute(select(Section.crn, Section.term)).all())

        current_rows, daily_rows, dropped = split_rows(records, known)

        upsert_latest(session, CurrentSeats, current_rows, ["crn", "term"])
        upsert_latest(session, SeatDaily, daily_rows, ["crn", "term", "day"])
        session.commit()

        totals = {
            "current_seats": session.execute(
                select(func.count()).select_from(CurrentSeats)).scalar(),
            "seat_daily": session.execute(
                select(func.count()).select_from(SeatDaily)).scalar(),
        }

    print(f"{len(records)} records from {len(paths)} file(s) -> "
          f"{len(current_rows)} sections, {len(daily_rows)} daily snapshots")
    print(f"totals: current_seats={totals['current_seats']} "
          f"seat_daily={totals['seat_daily']}")

    if dropped:
        total = sum(dropped.values())
        names = sorted(dropped)
        print(f"dropped {total} observations for {len(names)} course(s) not in "
              f"the catalogue: {names[:10]}{' …' if len(names) > 10 else ''}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
