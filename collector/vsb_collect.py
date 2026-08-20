#!/usr/bin/env python3
"""
Sweep every scheduled course for a term from VSB's class-data API.

WHY THIS ISN'T PLAYWRIGHT ANY MORE
  class-data is a plain REST endpoint. Driving it through a headless browser
  cost ~3.7s per course; httpx costs ~0.2s. The endpoint also accepts MANY
  courses per request via the indexed course_0_<n> parameters -- VSB is a
  schedule builder, so batching is what the API was built for. 80 courses came
  back in 0.56s in testing.

  Net effect: a full Fall sweep goes from ~3.5 hours to ~1-2 minutes.

  Playwright is still here, but only to open one page at startup and steal the
  t/e pair (see below). That is the single browser launch in the whole run.

THE t/e CHECKSUM
  class-data rejects requests without a valid `t` and `e`, replying "Please
  correct your device's timezone and time. It is off by N minutes." They encode
  the client's clock. Rather than reimplement VSB's javascript, we let the real
  page compute them once and reuse the pair for the sweep; it stays valid for
  at least several minutes. If a batch comes back with the timezone error we
  re-capture and retry.

COURSE SOURCE
  data/reference/courses-2026-2027.json, filtered to courses that actually have
  a schedule in the requested term. That's ~3,793 for Fall 2026. The catalogue
  is the system of record for what exists, so there is no separate watch list
  to keep in sync.

OUTPUT
  data/raw/<TERM>/batch_NNN.xml -- one file per request, each holding up to
  --batch-size courses. The directory is cleared and rewritten each sweep: raw
  is a snapshot of right now, and history lives in the parser's JSONL and the
  seat_watch table. parse_vsb.py already walks every <course> in a file, so
  multi-course documents need no parser change.

USAGE
  python3 vsb_collect.py                        # full Fall 2026 sweep
  python3 vsb_collect.py --term 202701          # Winter 2027
  python3 vsb_collect.py --limit 100            # first 100 courses, for testing
  python3 vsb_collect.py --courses COMP-250 MATH-133
"""

import argparse
import json
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
CATALOGUE = HERE.parent / "data" / "reference" / "courses-2026-2027.json"
RAW_DIR = HERE.parent / "data" / "raw"

API = "https://vsb.mcgill.ca/api/class-data"
CRITERIA = "https://vsb.mcgill.ca/criteria.jsp?term={term}&course_0_0={course}&nouser=1"

# Identify the client honestly -- this is a small university service, not a CDN.
HEADERS = {
    "User-Agent": "CourseFinder/0.1 (McGill student project; seat-availability research)",
    "Referer": "https://vsb.mcgill.ca/criteria.jsp",
}

# 80 worked comfortably in testing and the ceiling is higher, but 50 keeps each
# response ~100KB and each request under a second. Politeness, not a limit.
BATCH_SIZE = 50
BATCH_PAUSE = 1.0          # seconds between requests
TERM_MONTHS = {"01": "Winter", "05": "Summer", "09": "Fall"}


def term_label(code):
    """'202609' -> 'Fall 2026'. Matches the catalogue's schedule[].term."""
    season = TERM_MONTHS.get(code[4:])
    return f"{season} {code[:4]}" if season else None


def courses_for_term(code, catalogue=CATALOGUE):
    """
    Every course the catalogue schedules in this term.

    A course with no schedule entry for the term isn't offered, so asking VSB
    about it wastes a slot in the batch and returns nothing useful.
    """
    label = term_label(code)
    if label is None:
        return []

    records = json.loads(catalogue.read_text())
    return sorted(
        f'{r["subject"]}-{r["code"]}'
        for r in records
        if any(s["term"] == label for s in (r.get("schedule") or []))
    )


def capture_tokens(term, course):
    """
    Load one real VSB page and read the t/e pair off its class-data request.

    Imported lazily so the module still works (and starts fast) on machines
    without browsers installed, as long as tokens are supplied another way.
    """
    from playwright.sync_api import sync_playwright

    captured = {}

    def on_request(request):
        if "class-data" in request.url and "t" not in captured:
            params = httpx.URL(request.url).params
            captured["t"] = params.get("t")
            captured["e"] = params.get("e")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        page.on("request", on_request)
        try:
            page.goto(CRITERIA.format(term=term, course=course), timeout=30000)
            page.wait_for_timeout(3000)
        finally:
            browser.close()

    if "t" not in captured:
        raise RuntimeError("no class-data request seen -- did VSB change?")
    return captured["t"], captured["e"]


def build_params(term, courses, tokens):
    params = {
        "term": term,
        "nouser": "1",
        "t": tokens[0],
        "e": tokens[1],
        "_": str(int(time.time() * 1000)),
    }
    for i, course in enumerate(courses):
        params[f"course_0_{i}"] = course
        params[f"va_0_{i}"] = "undefined"
        params[f"rq_0_{i}"] = ""
    return params


def parse_response(text):
    """-> (course_keys, error_messages). Raises ET.ParseError on garbage."""
    root = ET.fromstring(text)
    keys = [c.get("key") for c in root.findall(".//course")]
    errors = [e.text for e in root.findall(".//errors/*") if e.text]
    return keys, errors


def stale_tokens(errors):
    """VSB's way of saying the t/e checksum no longer matches its clock."""
    return any("timezone" in e.lower() or "time" in e.lower() for e in errors)


def fetch_batch(client, term, courses, tokens):
    """
    One request for up to BATCH_SIZE courses.

    Returns (xml_text, course_keys) or (None, []) if the batch is unusable.
    Raises StaleTokens so the caller can re-capture and retry.
    """
    response = client.get(API, params=build_params(term, courses, tokens))
    response.raise_for_status()

    try:
        keys, errors = parse_response(response.text)
    except ET.ParseError as e:
        print(f"  not XML ({e})", file=sys.stderr)
        return None, []

    if stale_tokens(errors):
        raise StaleTokens(errors[0])
    if errors:
        print(f"  VSB errors: {errors}", file=sys.stderr)
    if not keys:
        return None, []

    return response.text, keys


class StaleTokens(Exception):
    pass


def sweep(term, courses, batch_size=BATCH_SIZE, replace=True):
    """
    Fetch every course in batches. Returns (files_written, courses_seen).

    `replace` clears the term directory first, because a previous sweep may
    have written more batches than this one and a leftover file would be
    loaded as if it were current. It's off for targeted runs (--courses), where
    wiping a full sweep's 76 files to fetch one course would be a nasty
    surprise; those write to their own batch numbers instead.
    """
    out_dir = RAW_DIR / term
    if replace and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tokens = capture_tokens(term, courses[0])
    print(f"tokens t={tokens[0]} e={tokens[1]}")

    batches = [courses[i:i + batch_size] for i in range(0, len(courses), batch_size)]
    written, seen = 0, set()
    started = time.time()

    with httpx.Client(headers=HEADERS, timeout=60) as client:
        for n, batch in enumerate(batches, 1):
            if n > 1:
                time.sleep(BATCH_PAUSE)

            for attempt in (1, 2):
                try:
                    text, keys = fetch_batch(client, term, batch, tokens)
                    break
                except StaleTokens as e:
                    print(f"  batch {n}: tokens expired ({e}) -- recapturing")
                    tokens = capture_tokens(term, courses[0])
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    print(f"  batch {n}: {type(e).__name__} on attempt {attempt}",
                          file=sys.stderr)
                    if attempt == 2:
                        text, keys = None, []
                    else:
                        time.sleep(2)
            else:
                text, keys = None, []

            if not text:
                print(f"  batch {n}/{len(batches)}: FAILED", file=sys.stderr)
                continue

            # Targeted runs use a distinct prefix so they never overwrite a
            # full sweep's batch_NNN.xml files.
            stem = "batch" if replace else "targeted"
            path = out_dir / f"{stem}_{n:03d}.xml"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(path)

            written += 1
            seen.update(keys)
            missing = len(batch) - len(keys)
            note = f" ({missing} not returned)" if missing else ""
            print(f"  batch {n}/{len(batches)}: {len(keys)} courses{note}")

    print(f"{written}/{len(batches)} batches, {len(seen)}/{len(courses)} courses, "
          f"{time.time() - started:.1f}s -> {out_dir}")
    return written, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--term", default="202609", help="VSB term code (default 202609)")
    ap.add_argument("--courses", nargs="*", help="specific course keys instead of a sweep")
    ap.add_argument("--limit", type=int, help="only the first N courses (testing)")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    if args.courses:
        courses = [c.strip().upper() for c in args.courses]
    else:
        courses = courses_for_term(args.term)
        if not courses:
            print(f"no catalogue courses scheduled in {args.term} "
                  f"({term_label(args.term)}) -- wrong term or wrong catalogue year",
                  file=sys.stderr)
            return 1

    if args.limit:
        courses = courses[:args.limit]

    print(f"{term_label(args.term)} ({args.term}): {len(courses)} courses, "
          f"batches of {args.batch_size}")

    written, seen = sweep(args.term, courses, args.batch_size,
                          replace=not args.courses)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
