"""
HTML routes: the pages a person or a crawler lands on.

Separate from routes/courses.py because these answer with documents, not data.
Same database, same services, entirely different contract -- one is consumed by
the app's own script, the other by browsers and search engines arriving cold.
"""

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as PathParam, Query, status
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import distinct, select

from app import seo
from app.dependencies.db import SessionDep
from app.models import Section
from app.search import DEFAULT_TERM, get_course, get_instructors, get_sections
from app.services import courses as course_service

router = APIRouter(include_in_schema=False)

FRONTEND = Path(os.getenv("FRONTEND_DIR", "frontend"))
_shell_cache: dict[str, str] = {}


def _shell():
    """
    index.html, read once.

    Deliberately cached: this is on the path of every crawled page, and re-reading
    an 80KB file per request to substitute two strings is work for nothing. The
    process restarts on deploy, which is the only time the file changes.
    """
    if "html" not in _shell_cache:
        _shell_cache["html"] = (FRONTEND / "index.html").read_text(encoding="utf-8")
    return _shell_cache["html"]


@router.get("/course/{course_id}", response_class=HTMLResponse)
def course_page(session: SessionDep,
                course_id: Annotated[str, PathParam(examples=["COMP-250"])],
                term: Annotated[str, Query()] = DEFAULT_TERM):
    """
    One course, at its own address, as real HTML.

    Serves the same document the app does, with the head rewritten for this
    course and the facts written into the list element. The script then boots
    normally and opens the course -- so this is a deep link for a person and an
    indexable page for a crawler, without maintaining two versions of anything.
    """
    course = get_course(session, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No course {course_id!r}")

    rating = course.avg_rating
    difficulty = course.avg_difficulty
    reviews = course.review_count
    detail = course_service.detail(session, course.id, term)
    sections = detail.sections if detail else []
    instructors = get_instructors(session, course.id, term)

    page = _shell()

    # Replace the generic head rather than appending to it: two <title> tags or
    # two og:title tags is not additive, and which one wins is not something to
    # leave to a crawler's mood.
    for tag in ('<title>McGill Course Finder — ratings and live seat counts</title>',
                '<meta name="description" content="Search every McGill course by rating, difficulty and live seat availability.">',
                '<meta property="og:title" content="McGill Course Finder">',
                '<meta property="og:description" content="Every McGill course — community ratings, difficulty, and live seat counts that update daily.">',
                '<meta property="og:url" content="https://mcgill-course-finder.fly.dev/">'):
        page = page.replace(tag, "", 1)
    page = page.replace("</head>",
                        seo.head_tags(course, rating, reviews, term) + "\n</head>", 1)

    # Into the element the app fills. The script clears it on first paint, so a
    # person sees this for one frame at most and a crawler sees all of it.
    page = page.replace(
        '<div class="list" id="rows"></div>',
        f'<div class="list" id="rows">'
        f'{seo.crawlable_block(course, rating, difficulty, reviews, sections, instructors, term)}'
        f'</div>', 1)

    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/sitemap.xml")
def sitemap(session: SessionDep):
    """
    Every course offered in either term.

    Scoped to what is actually offered, not the whole catalogue: a page for a
    course nobody can take is a page with nothing to say, and thin pages at
    scale are how a site earns a reputation for thin pages.
    """
    ids = session.execute(
        select(distinct(Section.course_id)).order_by(Section.course_id)).scalars().all()
    return Response(seo.sitemap(ids), media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return PlainTextResponse(seo.ROBOTS,
                             headers={"Cache-Control": "public, max-age=86400"})
