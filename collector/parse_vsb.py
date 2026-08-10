#!/usr/bin/env python3
"""
Parse VSB class-data XML into flat section records.

DESIGN NOTE
  We do NOT do a generic xml->json conversion. That would produce a nested
  structure mirroring VSB's internal model, which is not the shape you want to
  query. Instead we flatten to ONE RECORD PER SECTION (per CRN), which is the
  natural grain: a section is the thing that has seats, times, and a CRN.

  Raw XML stays on disk untouched. This output is derived and can be rebuilt
  at any time -- which is the whole point of keeping raw.

USAGE
  python3 parse_vsb.py data/raw/*.xml          # prints JSON to stdout
  python3 parse_vsb.py data/raw/*.xml --csv seats.csv   # also append seat obs
"""

import argparse
import csv
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# VSB encodes day-of-week as an integer. Evidence so far: a Tue/Thu course
# returned day=3 and day=5, which implies Sunday=1. VERIFY THIS by loading a
# course you know meets Mon/Wed/Fri and checking what comes back before you
# trust any schedule built on it.
DAY_NAMES = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}


def minutes_to_hhmm(mins):
    """VSB stores times as minutes from midnight. 605 -> '10:05'."""
    if mins is None:
        return None
    return f"{mins // 60:02d}:{mins % 60:02d}"


def as_int(value, default=None):
    """VSB uses '' and '-1' for 'not published'. Normalize to None."""
    if value in (None, "", "-1"):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_class_data(xml_text, fetched_at=None, source_file=None):
    """
    Turn one class-data XML response into a list of section dicts.

    Structure we're walking:
      <classdata date=...>
        <course key="MATH-254" code="MATH" number="254" faculty=...>
          <uselection>
            <selection>
              <block .../>           <- a section: CRN, seats, timeblockids
            </selection>
            <timeblock id="1" day="3" t1="605" t2="685"/>   <- sibling, not child
          </uselection>
          <offering title="..." desc="..."/>
        </course>
      </classdata>

    Note that <timeblock> is a SIBLING of <selection>, not nested inside the
    block that references it. The link is block@timeblockids -> timeblock@id.
    """
    root = ET.fromstring(xml_text)

    # VSB reports its own server time in epoch milliseconds. Prefer it over our
    # local clock -- it's the authoritative moment the data was true.
    classdata = root.find(".//classdata")
    if classdata is None:
        return []

    server_ms = as_int(classdata.get("date"))
    server_time = (
        datetime.fromtimestamp(server_ms / 1000, tz=timezone.utc).isoformat()
        if server_ms else None
    )

    term_el = classdata.find("term")
    term = term_el.get("n") if term_el is not None else None
    term_label = term_el.get("v") if term_el is not None else None

    records = []

    for course in classdata.findall("course"):
        offering = course.find("offering")
        title = offering.get("title") if offering is not None else None
        desc = offering.get("desc") if offering is not None else None

        for uselection in course.findall("uselection"):
            # Build the id -> timeblock lookup for THIS uselection only.
            # ids restart per uselection, so don't hoist this out of the loop.
            timeblocks = {}
            for tb in uselection.findall("timeblock"):
                timeblocks[tb.get("id")] = {
                    "day": as_int(tb.get("day")),
                    "day_name": DAY_NAMES.get(as_int(tb.get("day"))),
                    "start_min": as_int(tb.get("t1")),
                    "end_min": as_int(tb.get("t2")),
                    "start": minutes_to_hhmm(as_int(tb.get("t1"))),
                    "end": minutes_to_hhmm(as_int(tb.get("t2"))),
                    "date_from": as_int(tb.get("d1")),
                    "date_to": as_int(tb.get("d2")),
                }

            for selection in uselection.findall("selection"):
                for block in selection.findall("block"):
                    ids = [i for i in (block.get("timeblockids") or "").split(",") if i]
                    meetings = [timeblocks[i] for i in ids if i in timeblocks]

                    records.append({
                        # provenance -- always know where a row came from
                        "fetched_at": fetched_at,
                        "server_time": server_time,
                        "source_file": source_file,

                        # course identity
                        "term": term,
                        "term_label": term_label,
                        "course_key": course.get("key"),
                        "subject": course.get("code"),
                        "number": course.get("number"),
                        "title": title,
                        "description": desc,
                        "faculty": course.get("faculty"),

                        # section identity -- CRN is the primary key
                        "crn": block.get("key"),
                        "type": block.get("type"),
                        "section": block.get("secNo"),
                        "display": block.get("disp"),
                        "status": block.get("status"),
                        "credits": block.get("credits"),
                        "campus": block.get("campus"),
                        "location": block.get("location") or None,
                        "teacher": block.get("teacher") or None,   # usually empty
                        "note": block.get("n") or None,

                        # THE PERISHABLE PART -- this is why you collect daily
                        "open_seats": as_int(block.get("os")),
                        "max_enrol": as_int(block.get("me")),
                        "waitlist_seats": as_int(block.get("ws")),
                        "waitlist_count": as_int(block.get("wc")),
                        "is_full": block.get("isFull") == "1",
                        "non_reserved": as_int(block.get("nres")),

                        "meetings": meetings,
                    })

    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="raw XML files")
    ap.add_argument("--csv", help="append flat seat observations to this CSV")
    ap.add_argument("--out", help="write full JSON records here")
    args = ap.parse_args()

    all_records = []
    for path in args.files:
        p = Path(path)
        try:
            recs = parse_class_data(
                p.read_text(),
                # Fall back to file mtime if the filename has no timestamp.
                fetched_at=datetime.fromtimestamp(p.stat().st_mtime).isoformat(
                    timespec="seconds"),
                source_file=p.name,
            )
            all_records.extend(recs)
        except ET.ParseError as e:
            # A truncated or error response shouldn't kill the whole batch.
            print(f"skip {p.name}: {e}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(json.dumps(all_records, indent=2))
        print(f"wrote {len(all_records)} records to {args.out}", file=sys.stderr)
    else:
        print(json.dumps(all_records, indent=2))

    # The time series. One row per (observation, section). Append-only --
    # this file grows forever and that's exactly what you want.
    if args.csv:
        out = Path(args.csv)
        new = not out.exists()
        with out.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["server_time", "term", "course_key", "crn",
                            "type", "section", "open_seats",
                            "waitlist_count", "is_full"])
            for r in all_records:
                w.writerow([r["server_time"], r["term"], r["course_key"], r["crn"],
                            r["type"], r["section"], r["open_seats"],
                            r["waitlist_count"], int(r["is_full"])])
        print(f"appended {len(all_records)} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()