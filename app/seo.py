"""
Making the catalogue findable.

The app renders everything client-side, so a crawler arriving at "/" saw an
empty shell -- one course code in the whole document and no text. Nothing to
index means nothing to rank, which is a different problem from ranking badly and
has a different fix.

It also could not be linked. Opening a course changed no URL, so nobody could
send anyone "look at this one".

Both are the same missing piece: a real address per course, answering with real
HTML. The highest-intent search a student makes is "COMP 250 mcgill rating" --
this site has that answer and had no page that could ever appear in the result.

What this does NOT do is render the whole app server-side. The page a crawler
reads and the page a person uses are the same document: the server injects a
title, a description and a block of readable facts, then the script boots and
replaces that block with the live UI. One experience, indexable, deep-linkable.
"""

import html
import os
from datetime import date

from app.search import DEFAULT_TERM
from app.text import clean

SITE = os.getenv("SITE_URL", "https://mcgill-course-finder.fly.dev").rstrip("/")


def _esc(text):
    return html.escape(str(text or ""), quote=True)


def course_title(course):
    """"COMP 250 — Introduction to Computer Science" -- the phrase people type."""
    return f"{course.id.replace('-', ' ')} — {course.title}"


def course_description(course, rating=None, reviews=None):
    """
    The snippet under the search result, so it has to answer the query in the
    first hundred characters or so. Rating first when there is one, because
    "is this course any good" is the question being asked.
    """
    bits = []
    if rating:
        bits.append(f"Rated {rating:.1f}/5" + (f" from {reviews} reviews" if reviews else ""))
    if course.credits_text:
        bits.append(f"{course.credits_text} credits")
    if course.faculty:
        bits.append(course.faculty)
    lead = " · ".join(bits)
    # Straight off the ORM row, so it has not been through the schema's
    # validator -- a meta description full of anchor tags is worse than a
    # visible one, because nobody sees it to report it.
    body = clean(course.description or "").replace("\n", " ")
    text = f"{lead}. {body}" if lead else body
    return (text[:297] + "...") if len(text) > 300 else text


def head_tags(course, rating, reviews, term):
    """Title, description and social tags for one course."""
    t = _esc(course_title(course))
    d = _esc(course_description(course, rating, reviews))
    url = f"{SITE}/course/{_esc(course.id)}"
    return (
        f'<title>{t} | McGill Course Finder</title>\n'
        f'<meta name="description" content="{d}">\n'
        f'<link rel="canonical" href="{url}">\n'
        f'<meta property="og:type" content="article">\n'
        f'<meta property="og:title" content="{t}">\n'
        f'<meta property="og:description" content="{d}">\n'
        f'<meta property="og:url" content="{url}">\n'
        # Course is a real schema.org type, and it is what gets a result the
        # richer presentation in search rather than a plain blue link.
        f'<script type="application/ld+json">{_json_ld(course, rating, reviews, url)}</script>'
    )


def _json_ld(course, rating, reviews, url):
    import json
    data = {
        "@context": "https://schema.org",
        "@type": "Course",
        "name": course_title(course),
        "description": clean(course.description or "")[:500],
        "url": url,
        "courseCode": course.id,
        "provider": {"@type": "CollegeOrUniversity", "name": "McGill University"},
    }
    if rating and reviews:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(rating, 2),
            "reviewCount": reviews,
            "bestRating": 5,
            "worstRating": 1,
        }
    return json.dumps(data, separators=(",", ":"))


def crawlable_block(course, rating, difficulty, reviews, sections, instructors, term):
    """
    The facts, as plain HTML, inside the element the app later fills.

    A crawler reads this. A person never sees it -- the script replaces the
    element's contents on boot, before paint. It is not hidden text or a
    separate page served to robots: it is the same content the app shows, in the
    same place, just written by the server first.
    """
    rows = []
    if rating:
        rows.append(f"<li>Rating: {rating:.2f} out of 5"
                    + (f" from {reviews} student reviews" if reviews else "") + "</li>")
    if difficulty:
        rows.append(f"<li>Difficulty: {difficulty:.2f} out of 5</li>")
    if course.credits_text:
        rows.append(f"<li>Credits: {_esc(course.credits_text)}</li>")
    if course.faculty:
        rows.append(f"<li>Faculty: {_esc(course.faculty)}</li>")
    if course.department:
        rows.append(f"<li>Department: {_esc(course.department)}</li>")
    if instructors:
        rows.append(f"<li>Taught by: {_esc(', '.join(instructors))}</li>")
    if course.prerequisites_text:
        rows.append(f"<li>{_esc(clean(course.prerequisites_text))}</li>")

    seat_rows = ""
    for s in sections[:8]:
        when = _esc(s.schedule_text or "TBA")
        open_seats = "unknown" if s.open_seats is None else s.open_seats
        seat_rows += (f"<li>Section {_esc(s.section_no)} ({_esc(s.type_of_class)}), "
                      f"{when} — {open_seats} seats open</li>")

    return (
        f'<article><h1>{_esc(course_title(course))}</h1>'
        f'<p>{_esc(clean(course.description or ""))}</p>'
        f'<ul>{"".join(rows)}</ul>'
        + (f'<h2>Sections offered</h2><ul>{seat_rows}</ul>' if seat_rows else "")
        + '</article>'
    )


def sitemap(course_ids, changed=None):
    """
    Every course, one URL each.

    Search engines will not discover ten thousand pages that nothing links to,
    and the app links to none of them -- it is one document that rewrites
    itself. This is the only thing that tells a crawler they exist.
    """
    changed = (changed or date.today()).isoformat()
    urls = [f"<url><loc>{SITE}/</loc><changefreq>daily</changefreq>"
            f"<priority>1.0</priority></url>"]
    for cid in course_ids:
        urls.append(
            f"<url><loc>{SITE}/course/{_esc(cid)}</loc>"
            f"<lastmod>{changed}</lastmod>"
            f"<changefreq>daily</changefreq><priority>0.7</priority></url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(urls) + "</urlset>")


ROBOTS = f"""User-agent: *
Allow: /

# The API answers with JSON and has a per-IP rate limit. A crawler walking it
# would burn that budget for pages no one can read anyway -- the same data is
# in /course/*, as HTML, which is what should be indexed.
Disallow: /courses?
Disallow: /sections/
Disallow: /docs
Disallow: /openapi.json

Sitemap: {SITE}/sitemap.xml
"""
