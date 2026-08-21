#!/usr/bin/env python3
"""
Load the static course catalogue into the courses table.

OWNERSHIP
  The catalogue is the system of record for everything in `courses` -- title,
  description, credits, faculty, ratings. VSB carries its own copies of some of
  these but they lose: VSB truncates titles ("Intro to Computer Science") and
  its `credits` is per-section, so tutorials report 0.0. Nothing in this file
  reads VSB data.

COLUMN OWNERSHIP
  This file writes descriptive fields only. avg_rating, avg_difficulty and
  review_count belong to update_ratings.py, which reads them from the live
  mcgill.courses API -- the static export has them all zeroed, so writing them
  from here wipes real data on every reload. One writer per column.

IDEMPOTENT
  Upsert, not insert. Re-running updates existing rows in place rather than
  raising on the primary key, so this is safe to run against a populated
  database when next year's catalogue drops. Dimensions upsert; facts
  (seat observations) insert-ignore. That split is deliberate.

USAGE
  python3 load_courses.py
  python3 load_courses.py --file data/reference/courses-2027-2028.json
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from app.db import engine
from app.models import Course, CourseInstructor, CoursePrerequisite, Section

# VSB term codes are <calendar year of the term><month>. Verified against live
# responses: 202609 -> "Fall 2026", 202701 -> "Winter 2027". Summer is inferred
# from the same pattern and hasn't been checked against a live response.
TERM_MONTHS = {"Winter": "01", "Summer": "05", "Fall": "09"}

HERE = Path(__file__).resolve().parent
CATALOGUE = HERE / "data" / "reference" / "courses-2026-2027.json"

# Postgres caps a statement at 65535 bound parameters. At 15 columns per row
# that's ~4300 rows, so all 10k in one statement would fail. Chunk well under it.
BATCH_SIZE = 1000

Session = sessionmaker(engine)


def parse_credits(text):
    """
    '3' -> (3, 3);  '0-15' -> (0, 15);  '.66' -> (0.66, 0.66)

    11 catalogue values are ranges rather than numbers -- variable-credit
    courses like independent studies. A fixed-credit course gets min == max so
    `credits_min <= 3 <= credits_max` works as one query for both kinds.
    """
    if not text:
        return None, None
    low, _, high = text.partition("-")
    return Decimal(low), Decimal(high or low)


def to_course_fields(rec):
    """
    One catalogue record -> one courses row, as a plain dict.

    Deliberately knows nothing about SQLAlchemy: it takes a dict and returns a
    dict, so it can be tested without a database.
    """
    credits_min, credits_max = parse_credits(rec["credits"])

    return {
        # subject + code, not a regex over _id: 170 ids have no digits at all
        # (EXTLDPBO) and 1,630 carry suffixes (ACCT645D1). These two fields are
        # already split, always populated, and unique across all 10,156 rows.
        "id": f'{rec["subject"]}-{rec["code"]}',
        "catalogue_id": rec["_id"],
        "subject": rec["subject"],
        "code": rec["code"],
        "title": rec["title"],
        "description": rec["description"] or None,   # 223 arrive as ""
        "credits_text": rec["credits"],
        "credits_min": credits_min,
        "credits_max": credits_max,
        "faculty": rec["faculty"],
        "department": rec["department"],
        "url": rec["url"],
        # avg_rating / avg_difficulty / review_count are DELIBERATELY ABSENT.
        #
        # They belong to update_ratings.py, which reads them from the live
        # mcgill.courses API. The static export in data/reference/ has every one
        # of them zeroed, so including them here means each catalogue reload
        # silently wipes real ratings -- COMP-251 went from 2,609 reviews to 0
        # exactly that way. An upsert refreshes every column it is given, so the
        # only safe rule is that one writer owns a column.
        #
        #   this file          -> descriptive fields (title, credits, requirements)
        #   update_ratings.py  -> avg_rating, avg_difficulty, review_count
        #
        # On a fresh database the rating columns stay NULL until
        # update_ratings.py runs. That is correct: unknown, not zero.

        # The prose is authoritative; the tree and the flat edges are both
        # lossy conveniences derived from the same source.
        "prerequisites_text": _text(rec.get("prerequisitesText")),
        "corequisites_text": _text(rec.get("corequisitesText")),
        "restrictions_text": _text(rec.get("restrictions")),
        "logical_prerequisites": rec.get("logicalPrerequisites"),
        "logical_corequisites": rec.get("logicalCorequisites"),
    }


def _text(value):
    """
    Requirement fields arrive as a string, a list of strings, or null.

    Coerced here rather than in the model so the column is always either text
    or NULL -- a list reaching a String column raises at flush time, several
    thousand rows into the batch, with nothing to say which record did it.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return "\n".join(str(v) for v in value if v) or None
    return str(value) or None


def to_prerequisite_rows(rec):
    """
    Catalogue record -> course_prerequisites edges.

    Deduped on the composite key: a code can appear in both the prerequisite
    and corequisite lists, and occasionally twice in one list. Postgres's
    ON CONFLICT handles collisions with EXISTING rows, not duplicates inside a
    single statement, so the dedupe has to happen here.

    No `leads_to` edges: the catalogue's `leadingTo` is fully derivable by
    inverting these (all 7,115 of its edges are prerequisite edges already), so
    storing it would be a second source of truth free to disagree.
    """
    course_id = f'{rec["subject"]}-{rec["code"]}'
    rows = {}
    for kind, key in (("prerequisite", "prerequisites"),
                      ("corequisite", "corequisites")):
        for code in rec.get(key) or []:
            code = (code or "").strip().upper()
            if not code:
                continue
            rows[(course_id, code, kind)] = {
                "course_id": course_id, "required_code": code, "kind": kind,
            }
    return list(rows.values())


def term_code(label):
    """'Fall 2026' -> '202609'. Returns None for a term shape we don't know."""
    season, _, year = label.partition(" ")
    month = TERM_MONTHS.get(season)
    return f"{year}{month}" if month and year.isdigit() else None


def to_instructor_rows(rec):
    """
    Catalogue record -> course_instructors rows.

    Grain is (course, term, name) because that is all either source gives us --
    see the CourseInstructor docstring for why per-section is not derivable.
    """
    course_id = f'{rec["subject"]}-{rec["code"]}'
    rows = {}
    for entry in rec["instructors"]:
        code = term_code(entry["term"])
        if not code or not entry["name"]:
            continue
        # A name can be listed twice for the same term; the PK won't take it.
        rows[(course_id, code, entry["name"])] = {
            "course_id": course_id,
            "term": code,
            "name": entry["name"],
        }
    return list(rows.values())


def to_section_rows(rec):
    """
    Catalogue record -> sections rows, one per (crn, term).

    The catalogue emits one block per meeting pattern, so the same (crn, term)
    can appear several times -- 849 do. Those repeats are not redundant: 295
    carry timeblocks the others lack, and 224 name a different room. So we merge
    a group rather than taking the first, unioning the meetings and keeping each
    one's own location.
    """
    course_id = f'{rec["subject"]}-{rec["code"]}'
    merged = {}

    for sched in rec.get("schedule") or []:
        code = term_code(sched["term"])
        if not code:
            continue

        for block in sched["blocks"]:
            key = (block["crn"], code)
            row = merged.get(key)
            if row is None:
                # "Lec 001" -> type "Lec", number "001". Anything unexpected
                # keeps the whole string as the number and leaves type null.
                kind, _, number = (block.get("display") or "").partition(" ")
                row = merged[key] = {
                    "crn": block["crn"],
                    "term": code,
                    "course_id": course_id,
                    "section_no": number or None,
                    "type_of_class": kind or None,
                    "campus": block.get("campus"),
                    "meetings": [],
                }

            for tb in block["timeblocks"]:
                meeting = {
                    "day": int(tb["day"]),
                    "start_min": int(tb["t1"]),
                    "end_min": int(tb["t2"]),
                    "location": block.get("location") or None,
                }
                if meeting not in row["meetings"]:
                    row["meetings"].append(meeting)

    return list(merged.values())


def upsert(session, model, rows, key_columns):
    """
    Insert rows, refreshing any that already exist. Returns rows submitted.

    ON CONFLICT DO UPDATE is one round trip per batch. The alternative --
    querying for each row and branching on whether it exists -- is thousands of
    extra round trips and still races with anything else writing.
    """
    if not rows:
        return 0

    updatable = [c for c in rows[0] if c not in key_columns]

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        stmt = insert(model).values(batch)
        if updatable:
            stmt = stmt.on_conflict_do_update(
                index_elements=key_columns,
                set_={c: stmt.excluded[c] for c in updatable},
            )
        else:
            # Every column is part of the key, so there is nothing to refresh.
            stmt = stmt.on_conflict_do_nothing(index_elements=key_columns)
        session.execute(stmt)

    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=CATALOGUE, type=Path,
                    help="catalogue JSON (default: 2026-2027)")
    args = ap.parse_args()

    if not args.file.exists():
        print(f"no catalogue at {args.file}", file=sys.stderr)
        return 1

    with open(args.file, encoding="utf-8") as f:
        records = json.load(f)

    courses = [to_course_fields(r) for r in records]

    # Guard the join key before writing: a duplicate here would mean two
    # different courses collapsing into one row, which is silent data loss.
    ids = [r["id"] for r in courses]
    if len(set(ids)) != len(ids):
        print(f"duplicate course ids in {args.file.name} -- refusing to load",
              file=sys.stderr)
        return 1

    instructors = [row for r in records for row in to_instructor_rows(r)]

    # Two courses never share a (crn, term), but a section can be reached from
    # only one course, so no cross-record merge is needed here.
    sections = [row for r in records for row in to_section_rows(r)]
    prereqs = [row for r in records for row in to_prerequisite_rows(r)]

    with Session() as session:
        # One transaction for the whole file. A crash mid-load rolls back
        # entirely rather than leaving the tables inconsistent with each other.
        # Order matters: both child tables point at courses.
        n_courses = upsert(session, Course, courses, ["id"])
        n_instructors = upsert(session, CourseInstructor, instructors,
                               ["course_id", "term", "name"])
        n_sections = upsert(session, Section, sections, ["crn", "term"])
        n_prereqs = upsert(session, CoursePrerequisite, prereqs,
                           ["course_id", "required_code", "kind"])
        session.commit()

    print(f"{n_courses} courses, {n_instructors} instructors, "
          f"{n_sections} sections, {n_prereqs} requirement edges "
          f"<- {args.file.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
