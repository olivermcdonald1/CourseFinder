"""
Search orchestration.

The only file here with reasoning in it: one search request becomes two or three
queries, and this decides which. Not HTTP (that's routes/), not SQL (that's
search.py).
"""

from app.schemas.courses import (CourseDetail, CourseSummary, FilterOptions,
                                 Requirement, SearchResponse, SectionSummary,
                                 Unlocks)
from app.search import (count_matching, filter_options, get_course,
                        get_instructors, get_requirements, get_sections,
                        get_unlocks, search_page)
from app.search import DEFAULT_TERM

# Filtering on any of these already excludes unreviewed courses, because
# `NULL <= 2.5` is NULL and WHERE only keeps TRUE. So reviewed_only is a no-op
# for such a query and the second count would always return 0 -- a wasted round
# trip producing a meaningless number.
REVIEW_DERIVED = ("max_difficulty", "min_rating", "min_reviews")

# Naming something is a lookup, not a browse. search.py drops the reviewed-only
# default in that case, so nothing is being hidden to report.
NAMING_FILTERS = ("subject", "course_id", "title", "instructor")


def search(session, filters):
    """CourseFilters -> SearchResponse."""
    kwargs = filters.model_dump(exclude_none=True)

    # Pull out everything that is NOT a filter. search_courses takes these as
    # named parameters, and count_matching must not see them at all --
    # _filter_clauses has no such parameters and raises TypeError.
    #
    # sort_by is the subtle one: it's ordering intent, not a predicate, so it
    # belongs to the ranking not the WHERE clause. `**kwargs` is untyped, so
    # nothing catches a leak here until runtime.
    limit = kwargs.pop("limit")
    offset = kwargs.pop("offset")
    sort_by = kwargs.pop("sort_by", None)

    total = count_matching(session, **kwargs)
    rows = search_page(session, limit=limit, offset=offset,
                       sort_by=sort_by, **kwargs)

    return SearchResponse(
        total=total,
        limit=limit,
        offset=offset,
        hidden_unreviewed=_hidden_count(session, kwargs, total),
        # Convert INSIDE the session scope. A Course handed back raw would raise
        # DetachedInstanceError when serialization touched a lazy attribute
        # after the dependency closed the session.
        courses=[_summary(row) for row in rows],
    )


def _summary(row):
    """One search row -> CourseSummary, with the seat rollup grafted on."""
    (course, n_sections, max_open, max_wait, observed_at,
     has_history, delta, days) = row
    return CourseSummary.model_validate(course).model_copy(update={
        "n_sections": n_sections,
        "open_seats": max_open,
        "waitlist_count": max_wait,
        "has_history": has_history,
        "observed_at": observed_at,
        "seats_delta": delta,
        "trend_days": days,
        # Guard days: a same-day span would divide by zero, and a rate over
        # zero days is meaningless anyway.
        "seats_per_day": (round(delta / days, 2)
                          if delta is not None and days else None),
    })


# VSB encodes day-of-week as an integer with Sunday = 1. Verified: a Tue/Thu
# course returns 3 and 5.
DAY_NAMES = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}


def _clock(minutes):
    """
    605 -> '10:05 am';  815 -> '1:35 pm'. VSB stores minutes from midnight.

    12-hour with a lowercase suffix, because that is how a McGill timetable is
    read aloud. No leading zero on the hour -- "1:35 pm" not "01:35 pm".
    """
    if minutes is None:
        return None
    hour, minute = divmod(minutes % (24 * 60), 60)
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def _schedule_text(meetings):
    """
    Meetings -> "Tue Thu 11:35-12:55", grouping days that share a time.

    A section usually meets at the same time on several days, so listing each
    meeting separately would read "Tue 11:35-12:55 · Thu 11:35-12:55" and take
    twice the width to say the same thing.
    """
    if not meetings:
        return None, None

    slots, rooms = {}, []
    for m in meetings:
        start, end = _clock(m.get("start_min")), _clock(m.get("end_min"))
        day = DAY_NAMES.get(m.get("day"))
        if day is None:
            continue
        slots.setdefault((start, end), []).append(day)
        if m.get("location") and m["location"] not in rooms:
            rooms.append(m["location"])

    order = list(DAY_NAMES.values())
    parts = []
    for (start, end), days in slots.items():
        days = sorted(set(days), key=order.index)
        if start and end:
            # "11:35-12:55 pm" rather than "11:35 am-12:55 pm" when both sides
            # share a suffix -- the repeated am/pm carries no information.
            a, b = start.rsplit(" ", 1), end.rsplit(" ", 1)
            when = (f"{a[0]}-{end}" if a[1] == b[1] else f"{start}-{end}")
        else:
            when = "time TBA"
        parts.append(f"{' '.join(days)} {when}")

    return (" · ".join(parts) or None), (", ".join(rooms) or None)


def detail(session, course_id, term=DEFAULT_TERM):
    """One course with its sections and instructors, or None if no such course.

    Three queries, fixed: the course, its sections joined to seats, its
    instructors. Returning None rather than raising keeps HTTP concerns in the
    route -- this function has no opinion about status codes."""
    course = get_course(session, course_id)
    if course is None:
        return None

    sections = []
    for section, seats in get_sections(session, course.id, term):
        when, rooms = _schedule_text(section.meetings)
        sections.append(SectionSummary(
            crn=section.crn,
            section_no=section.section_no,
            type_of_class=section.type_of_class,
            campus=section.campus,
            meetings=section.meetings,
            # seats is None when the section has never been swept -- which is
            # not the same as having no seats.
            open_seats=seats.open_seats if seats else None,
            waitlist_count=seats.waitlist_count if seats else None,
            waitlist_seats=seats.waitlist_seats if seats else None,
            is_full=seats.is_full if seats else None,
            observed_at=seats.observed_at if seats else None,
            schedule_text=when,
            rooms=rooms,
        ))

    # model_validate reads the Course columns; model_copy grafts on the parts
    # that don't live on that row.
    return CourseDetail.model_validate(course).model_copy(update={
        "term": term,
        "instructors": get_instructors(session, course.id, term),
        "sections": sections,
        "requirements": [
            Requirement(code=code, kind=kind, course_id=cid, title=title)
            for code, kind, cid, title in get_requirements(session, course.id)
        ],
        "unlocks": [
            Unlocks(course_id=cid, title=title, avg_rating=r, review_count=n)
            for cid, title, r, n in get_unlocks(session, course.id)
        ],
    })


def options(session, term=DEFAULT_TERM):
    """Option lists for the filter chips."""
    return FilterOptions.model_validate(filter_options(session, term))


def _hidden_count(session, kwargs, total):
    """
    How many courses the reviewed-only default is hiding.

    Costs a third query, so only run it when the answer can be non-zero. The UI
    uses it to make the boundary visible ("2,719 without reviews hidden")
    rather than mysterious.
    """
    if any(kwargs.get(k) for k in NAMING_FILTERS):
        return 0
    if any(k in kwargs for k in REVIEW_DERIVED):
        return 0
    if kwargs.get("reviewed_only") is False:
        return 0

    unfiltered = count_matching(session, **{**kwargs, "reviewed_only": False})
    return unfiltered - total
