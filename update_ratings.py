#!/usr/bin/env python3
"""
Fill courses.avg_rating / avg_difficulty / review_count from mcgill.courses.

WHY THIS EXISTS SEPARATELY FROM load_courses.py
  The static catalogue export in data/reference/ has every rating zeroed --
  COMP-250 shows 0 reviews locally and 3,941 on the live API. Rather than
  re-fetch and re-load the whole catalogue, this patches three columns on rows
  that already exist. Everything else (titles, credits, schedules, sections)
  stays exactly as loaded.

IT DOES NOT TOUCH THEIR DATABASE
  mcgill.courses exposes an HTTP API, nothing more. This pages that API into
  Python dicts, then runs UPDATEs against OUR Postgres using catalogue_id as
  the join key -- the API's `_id` ("COMP250") is what we stored in
  courses.catalogue_id when the catalogue was first loaded.

THIS FILE OWNS THREE COLUMNS
  avg_rating, avg_difficulty, review_count. load_courses.py deliberately does
  not write them -- the static catalogue export has them zeroed, so a reload
  would silently wipe everything patched here. Run this after any catalogue
  reload.

UNREVIEWED COURSES GET NULL, NOT 0.0
  The API reports 0.0 rating for a course nobody has reviewed. Stored as 0.0
  that sorts *below* a genuinely terrible course, and "easy courses" would
  surface the unreviewed long tail. NULL means "unknown", which is the truth
  and which ORDER BY handles sensibly.

BE A GOOD CITIZEN
  mcgill.courses is a student project like this one, and it rate-limits: paging
  at 100/request returned 429 after ~128 requests. It accepts limit=1000, so the
  whole catalogue is 16 requests instead of 155 -- fewer, larger requests are
  both faster and roughly 10x less load than politely-spaced small ones. 429s
  are still honoured with backoff in case the limit tightens.

  Run this WEEKLY. Ratings accumulate over weeks; there is nothing to gain from
  running it more often, and it must not go on the seat-sweep schedule.

USAGE
  python3 update_ratings.py --dry-run     # fetch and report, write nothing
  python3 update_ratings.py
"""

import argparse
import sys
import time

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.db import engine
from app.models import Course

API = "https://mcgill.courses/api/courses"
HEADERS = {
    "User-Agent": "CourseFinder/0.1 (McGill student project; course search)",
}
# Their maximum accepted page size, as far as probing showed. 16 requests for
# the whole catalogue; at 100 the run hit a 429 partway through.
PAGE_SIZE = 1000
PAGE_PAUSE = 0.5           # seconds between requests
MAX_RETRIES = 3
BATCH_SIZE = 1000

Session = sessionmaker(engine)


def fetch_page(client, offset):
    """
    One page, backing off on 429.

    Honours Retry-After when the server sends it and falls back to exponential
    backoff when it doesn't -- a fixed sleep would just walk into the limit
    again at a slower pace.
    """
    for attempt in range(MAX_RETRIES):
        response = client.get(API, params={"limit": PAGE_SIZE, "offset": offset})

        if response.status_code == 429:
            wait = int(response.headers.get("retry-after") or 2 ** (attempt + 3))
            print(f"\n  rate limited at offset {offset}, waiting {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
            continue


        response.raise_for_status()
        return response.json()["courses"]

    raise httpx.HTTPError(f"still rate limited after {MAX_RETRIES} attempts")


def fetch_ratings(client):
    """
    Page the whole catalogue -> {catalogue_id: (rating, difficulty, reviews)}.
    Pages until a short page comes back rather than looping to a hardcoded
    count: the course count changes, and a fixed range would silently stop
    short the day they add courses.
    """
    ratings = {}
    offset = 0

    while True:
        courses = fetch_page(client, offset)
        if not courses:
            break

        for c in courses:
            ratings[c["_id"]] = (
                c.get("avgRating"),
                c.get("avgDifficulty"),
                c.get("reviewCount") or 0,
            )

        offset += len(courses)
        if len(courses) < PAGE_SIZE:
            break

        print(f"\r  fetched {offset}...", end="", file=sys.stderr, flush=True)
        time.sleep(PAGE_PAUSE)

    print(f"\r  fetched {offset} courses ", file=sys.stderr)
    return ratings


def to_update_rows(existing, ratings):
    """
    (our courses) x (their ratings) -> rows for a bulk UPDATE.

    `existing` is [(id, catalogue_id), ...] from our database. The join happens
    here, in Python, on catalogue_id -- there is no cross-database query.
    Courses with no match are left untouched rather than zeroed.
    """
    rows, unmatched, reviewed = [], [], 0

    for course_id, catalogue_id in existing:
        found = ratings.get(catalogue_id)
        if found is None:
            unmatched.append(course_id)
            continue

        rating, difficulty, review_count = found
        if review_count > 0:
            reviewed += 1
        else:
            # No reviews means no opinion, not a rating of zero.
            rating = difficulty = None

        rows.append({
            "id": course_id,
            "avg_rating": rating,
            "avg_difficulty": difficulty,
            "review_count": review_count,
        })

    return rows, unmatched, reviewed


def apply_updates(session, rows):
    """
    Bulk UPDATE by primary key.

    Each dict carries `id` plus the columns to set; SQLAlchemy turns the list
    into an executemany rather than one round trip per row. Rows whose id does
    not exist are silently skipped, which is why to_update_rows() builds from
    OUR course list rather than theirs.
    """
    for start in range(0, len(rows), BATCH_SIZE):
        session.execute(update(Course), rows[start:start + BATCH_SIZE])
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report coverage, write nothing")
    args = ap.parse_args()

    print(f"fetching {API}", file=sys.stderr)
    try:
        with httpx.Client(headers=HEADERS, timeout=30) as client:
            ratings = fetch_ratings(client)
    except httpx.HTTPError as e:
        print(f"fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if not ratings:
        print("no courses returned", file=sys.stderr)
        return 1

    with Session() as session:
        existing = session.execute(select(Course.id, Course.catalogue_id)).all()
        rows, unmatched, reviewed = to_update_rows(existing, ratings)

        if not args.dry_run:
            apply_updates(session, rows)
            session.commit()

    print(f"their catalogue : {len(ratings)} courses")
    print(f"ours            : {len(existing)} courses")
    print(f"matched         : {len(rows)} ({reviewed} with at least one review)")
    print(f"unmatched       : {len(unmatched)}"
          + (f"  e.g. {unmatched[:5]}" if unmatched else ""))
    print("dry run -- nothing written" if args.dry_run
          else f"updated {len(rows)} courses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
