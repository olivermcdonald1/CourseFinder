"""
Course search over the loaded catalogue and live seat data.

Phase 1: pure SQL, no LLM. The LLM layer's only job (Phase 2) is turning a
sentence into the keyword arguments below -- it never sees course rows and never
writes prose, which is what keeps the token bill near zero.

SECTION FILTERS APPLY TO THE SAME SECTION
  campus, seats, waitlist and class type are all properties of a SECTION, not a
  course. They go into one EXISTS together, so "a Downtown section that has
  seats" means one section satisfying both -- not a course that happens to have
  a full Downtown lecture and an open Macdonald one 30km away. Splitting them
  into separate EXISTS clauses would quietly return the wrong courses.

REVIEWED-ONLY BY DEFAULT
  7,698 of 10,156 courses have no reviews, so avg_rating and avg_difficulty are
  NULL for three quarters of the catalogue -- overwhelmingly grad seminars,
  professional programmes (DENT) and one-on-one music instruction (MUIN). A
  browsing query defaults to reviewed courses only.

  The exception is a query that NAMES something: "MUIN courses" or "COMP-250" is
  a lookup, not a browse, and hiding the answer would be wrong. Leave
  reviewed_only=None and the default logic notices a subject or id was given.

NULLS LAST IS NOT OPTIONAL
  Postgres sorts NULL as larger than any value, so `ORDER BY avg_rating DESC`
  returns 7,698 unreviewed courses before anything real.
"""

import re

from sqlalchemy import (Integer, and_, case, distinct, exists, func, or_,
                        select)

from app.models import (Course, CourseInstructor, CoursePrerequisite,
                        CurrentSeats, SeatDaily, Section)

DEFAULT_TERM = "202609"          # Fall 2026
DEFAULT_LIMIT = 50

UNDERGRAD_MAX_LEVEL = 400

# Weight of the prior in the damped rating: how many "average" reviews a course
# is credited with before its own reviews start to count. Roughly the median
# review count. Higher = more sceptical of small samples.
PRIOR_WEIGHT = 20

SORT_OPTIONS = ("rating", "easiest", "popular", "seats")


def _earliest_meeting_min():
    """
    The earliest start time, in minutes from midnight, across a section's
    meetings -- as a scalar subquery over the JSONB array.

    `meetings` is a JSON array of {day, start_min, end_min, location}, so this
    unnests it and takes the min. Sections with no meetings (async, TBA) return
    NULL, which means a "nothing before 10am" filter excludes them: an unknown
    time can't be promised to be late.
    """
    element = func.jsonb_array_elements(Section.meetings).alias("m")
    return (select(func.min(
                func.cast(element.column.op("->>")("start_min"), Integer)))
            .select_from(element)
            .scalar_subquery())


def _section_exists(term, *, campus=None, class_type=None,
                    open_seats=False, max_waitlist=None, earliest_start=None):
    """
    "This course has a section in `term` satisfying ALL of these."

    EXISTS rather than JOIN: MATH-133 has 22 sections, and a join would return
    the course 22 times. DISTINCT would paper over it while forcing the planner
    to dedupe every wide row and breaking LIMIT (you'd limit after dedup rather
    than before). EXISTS stops at the first match and never multiplies.

    The term filter belongs INSIDE the subquery: outside it would decide which
    courses qualify, inside it decides which sections count.
    """
    clauses = [Section.course_id == Course.id, Section.term == term]

    if campus is not None:
        clauses.append(Section.campus.ilike(campus))
    if class_type is not None:
        clauses.append(Section.type_of_class.ilike(class_type))
    if earliest_start is not None:
        # No meeting in this section may begin before earliest_start.
        clauses.append(_earliest_meeting_min() >= earliest_start)

    # Only correlate to current_seats when a seat condition was actually asked
    # for -- otherwise a section with no observation yet would be excluded.
    if open_seats or max_waitlist is not None:
        clauses += [CurrentSeats.crn == Section.crn,
                    CurrentSeats.term == Section.term]
        if open_seats:
            clauses.append(CurrentSeats.open_seats > 0)
        if max_waitlist is not None:
            clauses.append(CurrentSeats.waitlist_count <= max_waitlist)

    return exists().where(and_(*clauses))


def _taught_by(name, term=None):
    """
    Courses this instructor teaches. EXISTS for the same reason: a course with
    three listed instructors would triple in a join.

    Substring match, because users type "Alberini" not "Giulia Alberini".
    """
    clauses = [CourseInstructor.course_id == Course.id,
               CourseInstructor.name.ilike(f"%{name}%")]
    if term is not None:
        clauses.append(CourseInstructor.term == term)
    return exists().where(and_(*clauses))


def _keyword_clauses(text, term=None):
    """
    Free-text matching: every token must appear SOMEWHERE, in any field.

    A single `ILIKE '%whole query%'` only matches a contiguous substring, so
    "intro comp" found nothing even though "Introduction to Computer Science" is
    obviously it. Tokenising fixes that:

        AND over tokens, OR over fields

    Fields include subject and both id spellings, so "math" matches every MATH
    course *and* anything with "math" in its title -- which is what someone
    typing four letters actually wants, rather than an exact subject-code match
    that returns nothing for "calc".

    Instructor name is one of those fields, and it was the omission that showed
    up first in real use: someone searched "Mckeague", twice, and got nothing --
    while `instructor=Mckeague` returned CHEM-110 immediately. The data was
    there and the box simply never looked at it. Who teaches a course is one of
    the few things people already know before they know the course code, so it
    belongs in the same guess as the rest.

    Capped at 8 tokens so a pasted paragraph can't build 40 OR-groups.
    """
    clauses = []
    for token in _tokens(text):
        pattern = f"%{token}%"
        clauses.append(or_(
            Course.id.ilike(pattern),            # COMP-250
            Course.catalogue_id.ilike(pattern),  # COMP250
            Course.subject.ilike(pattern),
            Course.title.ilike(pattern),
            Course.description.ilike(pattern),
            _taught_by(token, term),             # "Mckeague", "muth"
        ))
    return clauses


def _tokens(text):
    return [t for t in re.split(r"\s+", (text or "").strip()) if t][:8]


def _relevance(text):
    """
    Which field matched decides the order: specific beats broad.

        0  the code            "COMP 250" -> COMP-250
        1  the title           every token in the course name
        2  the subject         "math" -> all MATH courses
        3  description only    the token is buried in the blurb

    Without this, searching "climate" ranked "The Italian Renaissance" (whose
    description mentions the word) alongside "Climate Physics". Tier 1 requires
    ALL tokens in the title, so "intro comp" still lands on Introduction to
    Computer Science rather than on whatever mentions both words in prose.
    """
    tokens = _tokens(text)
    if not tokens:
        return None

    joined = f"%{' '.join(tokens)}%"
    squashed = f"%{''.join(tokens)}%"

    all_in_title = and_(*[Course.title.ilike(f"%{t}%") for t in tokens])
    any_subject = or_(*[Course.subject.ilike(f"%{t}%") for t in tokens])

    return case(
        (or_(Course.id.ilike(squashed), Course.catalogue_id.ilike(squashed),
             Course.title.ilike(joined)), 0),
        (all_in_title, 1),
        (any_subject, 2),
        else_=3,
    ).asc()


def _level_char(level):
    """
    400 -> '4'. A course's level is the first character of its code.

    Compared as a CHARACTER rather than cast to int, because `code` isn't always
    numeric -- EXTL-DPBO has code 'DPBO'. In ASCII 'D' sorts above '4', so
    non-numeric codes fall outside any undergrad range naturally instead of
    raising on the cast.
    """
    return str(level // 100)


def _filter_clauses(
    *,
    # identity
    course_id=None,
    subject=None,
    title=None,
    keywords=None,
    # academic shape
    credits=None,
    credits_any=None,
    levels=None,
    min_level=None,
    max_level=None,
    undergrad_only=False,
    faculty=None,
    department=None,
    instructor=None,
    # reputation
    max_difficulty=None,
    min_rating=None,
    min_reviews=None,
    reviewed_only=None,
    # offering / logistics
    term=DEFAULT_TERM,
    campus=None,
    class_type=None,
    has_open_seats=False,
    max_waitlist=None,
    earliest_start=None,
    has_history=False,
    offered_only=True,
):
    """
    Build the WHERE clauses shared by search and count.

    Separate so counting doesn't fetch rows: both callers apply the same
    predicates, then one adds ORDER BY + LIMIT and the other wraps it in
    count(). Counting by len()-ing the search results would drag every matching
    row across the wire to throw it away.
    """
    if reviewed_only is None:
        reviewed_only = not (subject or course_id or title or instructor)

    clauses = []

    # `is not None` throughout, never truthiness. credits=0, max_difficulty=0
    # and max_waitlist=0 are all meaningful -- `if credits:` would drop them,
    # and the catalogue really does contain 0-credit courses.
    if course_id is not None:
        clauses.append(Course.id == course_id.upper())
    if subject is not None:
        clauses.append(Course.subject == subject.upper())
    if title is not None:
        clauses.append(Course.title.ilike(f"%{title}%"))
    if keywords is not None:
        # Scoped to the term being browsed, so a professor who taught this in a
        # different term does not drag the course into a list of what is on
        # offer now.
        clauses.extend(_keyword_clauses(keywords, term))

    if credits is not None:
        # min == max for fixed-credit courses, so these two predicates cover
        # both them and variable-credit ranges like "0-15".
        clauses.append(Course.credits_min <= credits)
        clauses.append(Course.credits_max >= credits)
    if credits_any:
        # A course spans credits_min..credits_max, so "3 or 4" means its span
        # covers any of the chosen values -- not that it equals one of them.
        clauses.append(or_(*[
            and_(Course.credits_min <= v, Course.credits_max >= v)
            for v in credits_any]))
    # An explicit set of levels, rather than a span. min/max can only express a
    # contiguous range, so "100 and 300" would quietly drag in every 200-level
    # course -- wrong in a way nobody would catch by looking. Two separate users
    # were watched clicking 100, then 200, then 300, then 400 one at a time,
    # which is the workaround this removes.
    if levels:
        clauses.append(func.left(Course.code, 1).in_(
            [_level_char(v) for v in levels]))
    elif undergrad_only and max_level is None:
        # An explicit level set already says which levels are wanted, so the
        # undergrad ceiling would only be able to contradict it.
        max_level = UNDERGRAD_MAX_LEVEL
    if min_level is not None:
        clauses.append(func.left(Course.code, 1) >= _level_char(min_level))
    if max_level is not None:
        clauses.append(func.left(Course.code, 1) <= _level_char(max_level))
    if faculty is not None:
        clauses.append(Course.faculty.ilike(f"%{faculty}%"))
    if department is not None:
        clauses.append(Course.department.ilike(f"%{department}%"))
    if instructor is not None:
        clauses.append(_taught_by(instructor, term))

    if max_difficulty is not None:
        clauses.append(Course.avg_difficulty <= max_difficulty)
    if min_rating is not None:
        clauses.append(Course.avg_rating >= min_rating)
    if min_reviews is not None:
        clauses.append(Course.review_count >= min_reviews)
    if reviewed_only:
        clauses.append(Course.review_count > 0)
    if has_history:
        # Courses where seat movement is calculable -- the one thing no other
        # McGill tool can answer.
        clauses.append(Course.id.in_(select(_hist_agg(term).c.cid)))

    # One EXISTS for every section-level condition, so they describe the same
    # section rather than three unrelated ones.
    needs_section = (campus is not None or class_type is not None
                     or has_open_seats or max_waitlist is not None
                     or earliest_start is not None)
    if needs_section:
        clauses.append(_section_exists(
            term, campus=campus, class_type=class_type,
            open_seats=has_open_seats, max_waitlist=max_waitlist,
            earliest_start=earliest_start))
    elif offered_only:
        clauses.append(_section_exists(term))

    return clauses


def _global_mean_rating():
    """
    Average rating across reviewed courses, as a scalar subquery.

    Computed in SQL rather than hardcoded so it tracks the data -- Postgres
    evaluates it once per statement (an InitPlan), not once per row.
    """
    return (select(func.avg(Course.avg_rating))
            .where(Course.review_count > 0)
            .scalar_subquery())


def _damped_rating():
    """
    Bayesian average: a course's own rating pulled toward the global mean in
    proportion to how little evidence it has.

        (W * mean + n * rating) / (W + n)

    n=2 sits almost entirely at the mean; n=760 keeps essentially its own
    average. Without this, SEAD-515 (2 reviews, 1.0) and any course with a
    single 5.0 outrank ITAL-206 (760 reviews, 4.83), which is the single most
    visible way a course search can look broken.

    Unreviewed courses yield NULL (n=0 and rating NULL), so they sort last
    rather than landing at the mean -- reviewed courses come first by design.
    """
    n = Course.review_count
    return ((PRIOR_WEIGHT * _global_mean_rating() + n * Course.avg_rating)
            / (PRIOR_WEIGHT + n))


def _max_open_seats(term):
    """
    Most open seats across any of the course's sections this term.

    Correlated scalar subquery: evaluated per candidate course, referencing
    Course.id from the outer query. Used only for sort_by="seats".
    """
    return (select(func.max(CurrentSeats.open_seats))
            .where(and_(Section.course_id == Course.id,
                        Section.term == term,
                        CurrentSeats.crn == Section.crn,
                        CurrentSeats.term == Section.term))
            .correlate(Course)
            .scalar_subquery())


def _order_by(filters, sort_by):
    """
    Relevance first, then quality. Filters never appear here.

    A filter is satisfied or not -- re-sorting by a dimension you already
    filtered on double-counts it. Ask for "difficulty under 2.5" and sort by
    difficulty, and the 1.0-difficulty grad seminar beats the 2.4-difficulty
    course with 800 happy reviews, which is not what anyone wanted.

    `sort_by` is a separate axis for superlative intent ("easiest", "best
    rated"), which is a different request from a threshold.
    """
    order = []

    # Tier 1 -- relevance: code, then title, then subject, then description.
    # Silent when nothing was typed, so a pure chip query is ranked by quality
    # alone.
    relevance = _relevance(filters.get("keywords"))
    if relevance is not None:
        order.append(relevance)

    # Tier 2 -- quality, or explicit sort intent.
    if sort_by == "easiest":
        order.append(Course.avg_difficulty.asc().nulls_last())
    elif sort_by == "popular":
        order.append(Course.review_count.desc())
    elif sort_by == "seats":
        order.append(_max_open_seats(filters.get("term", DEFAULT_TERM))
                     .desc().nulls_last())
    else:                                    # None or "rating"
        order.append(_damped_rating().desc().nulls_last())

    # Tier 3 -- tiebreak. More reviews means more confidence in the ordering
    # above it.
    order.append(Course.review_count.desc())

    # Tier 4 -- the primary key, purely to make the ordering TOTAL.
    #
    # Not cosmetic: 166 groups of courses share both a rating and a review
    # count exactly (GERM-200 and HIST-437 are both 5.00 from 76 reviews). With
    # ties, Postgres gives no guarantee of relative order between two separate
    # queries -- so with OFFSET pagination the same course could appear on page
    # 1 and page 2 while another was skipped entirely. Observed, not theorised.
    #
    # Any unique column fixes it; the PK is the obvious one.
    order.append(Course.id.asc())
    return order


def search_courses(session, *, limit=DEFAULT_LIMIT, offset=0, sort_by=None,
                   **filters):
    """
    Find courses matching any combination of filters. All filters are optional.

    Keyword-only so call sites read as search_courses(session, credits=3, ...);
    a row of positional booleans would be unreadable and easy to transpose.

    sort_by is deliberately NOT a filter -- see _order_by.

    OFFSET APPLIES AFTER ORDER BY, like LIMIT. That's what makes pagination
    stable: page 2 is ranks 51-100 of the same ordering, not 50 arbitrary rows.

    Returns a list of Course objects.
    """
    if sort_by is not None and sort_by not in SORT_OPTIONS:
        raise ValueError(f"sort_by must be one of {SORT_OPTIONS}, got {sort_by!r}")

    stmt = (select(Course)
            .where(*_filter_clauses(**filters))
            .order_by(*_order_by(filters, sort_by))
            .offset(offset)
            .limit(limit))
    return session.execute(stmt).scalars().all()


def _seat_agg(term):
    """
    Per-course seat rollup for a term, as a subquery.

    One grouped pass over sections rather than a scalar subquery per row: the
    planner can hash-join it against whatever the filters leave behind.

    OUTER join to current_seats so a section we haven't swept yet still counts
    toward n_sections -- inner would make unswept sections vanish from the
    count, which reads as "this course has fewer sections than it does".
    """
    return (select(Section.course_id.label("cid"),
                   func.count().label("n_sections"),
                   func.max(CurrentSeats.open_seats).label("max_open"),
                   func.max(CurrentSeats.waitlist_count).label("max_wait"),
                   # Newest sweep across the course's sections. Data age is what
                   # makes a live number trustworthy -- "13 open" means nothing
                   # without knowing whether that was measured an hour or a
                   # month ago.
                   func.max(CurrentSeats.observed_at).label("observed_at"))
            .outerjoin(CurrentSeats, and_(CurrentSeats.crn == Section.crn,
                                          CurrentSeats.term == Section.term))
            .where(Section.term == term)
            .group_by(Section.course_id)
            .subquery())


def _hist_agg(term):
    """
    Courses with at least two days of seat snapshots -- i.e. where movement is
    calculable at all. HAVING, because the condition is on the aggregate.
    """
    return (select(Section.course_id.label("cid"))
            .join(SeatDaily, and_(SeatDaily.crn == Section.crn,
                                  SeatDaily.term == Section.term))
            .where(Section.term == term)
            .group_by(Section.course_id)
            .having(func.count(distinct(SeatDaily.day)) >= 2)
            .subquery())


def _trend_agg(term):
    """
    Per-course seat movement: total seats gained or lost across all its
    sections, and the span observed.

    Two DISTINCT ON subqueries pick the first and last snapshot per section --
    Postgres's DISTINCT ON keeps the first row of each group under the given
    ORDER BY, which is the cheapest way to say "earliest" and "latest" without
    a window function or a correlated max().

    Summed per course rather than per section, because a card shows one number:
    "this course lost 14 seats" is the useful claim, not fourteen separate
    section deltas.
    """
    def edge(direction):
        return (select(SeatDaily.crn, SeatDaily.term, SeatDaily.day,
                       SeatDaily.open_seats)
                .where(SeatDaily.term == term)
                .distinct(SeatDaily.crn, SeatDaily.term)
                .order_by(SeatDaily.crn, SeatDaily.term, direction)
                .subquery())

    first, last = edge(SeatDaily.day.asc()), edge(SeatDaily.day.desc())

    return (select(
                Section.course_id.label("cid"),
                func.sum(last.c.open_seats - first.c.open_seats).label("seats_delta"),
                func.max(last.c.day - first.c.day).label("trend_days"))
            .join(first, and_(first.c.crn == Section.crn,
                              first.c.term == Section.term))
            .join(last, and_(last.c.crn == Section.crn,
                             last.c.term == Section.term))
            .where(Section.term == term)
            .group_by(Section.course_id)
            .subquery())


def search_page(session, *, limit=DEFAULT_LIMIT, offset=0, sort_by=None,
                **filters):
    """
    Like search_courses, but each row carries its seat rollup.

    Returns (Course, n_sections, max_open, max_wait, has_history, seats_delta,
    trend_days, total) rows. Separate
    from search_courses so that function keeps its simple list[Course] contract
    for scripts and tests; both share _filter_clauses and _order_by, so there is
    one definition of what a filter means.

    `total` is how many rows matched before LIMIT, carried on every row by a
    window function. It used to be a second query, and against a database ~75ms
    away a second query costs far more than a second column does -- the count is
    computed by the same scan either way, so this asks for it rather than going
    back to ask again. Window functions run after WHERE and before LIMIT, which
    is exactly the number the pager needs.
    """
    if sort_by is not None and sort_by not in SORT_OPTIONS:
        raise ValueError(f"sort_by must be one of {SORT_OPTIONS}, got {sort_by!r}")

    term = filters.get("term", DEFAULT_TERM)
    seats, hist, trend = _seat_agg(term), _hist_agg(term), _trend_agg(term)

    stmt = (select(Course,
                   seats.c.n_sections, seats.c.max_open, seats.c.max_wait,
                   seats.c.observed_at,
                   hist.c.cid.isnot(None).label("has_history"),
                   trend.c.seats_delta, trend.c.trend_days,
                   # The score the list is actually ORDERED by. Sending only the
                   # raw average meant the page contradicted itself: a course
                   # showing 4.98 sat above three showing 5.00, because 938
                   # reviews outweigh 153. Correct, and indistinguishable from a
                   # broken sort unless the number you rank by is the number you
                   # show.
                   _damped_rating().label("weighted_rating"),
                   func.count().over().label("total"))
            .outerjoin(seats, seats.c.cid == Course.id)
            .outerjoin(hist, hist.c.cid == Course.id)
            .outerjoin(trend, trend.c.cid == Course.id)
            .where(*_filter_clauses(**filters))
            .order_by(*_order_by(filters, sort_by))
            .offset(offset).limit(limit))
    return session.execute(stmt).all()


def get_course(session, course_id):
    """One course by id, or None. Case-insensitive on the caller's behalf."""
    return session.get(Course, course_id.upper())


def get_sections(session, course_id, term):
    """
    A course's sections for a term, each paired with its live seat row.

    LEFT OUTER JOIN, not inner: a section we have not swept yet still exists and
    should still be listed. An inner join would silently hide it.

    Returned as (Section, CurrentSeats | None) tuples. There is no
    relationship() on the models -- only foreign keys -- so there is no lazy
    loading to trigger and no N+1 to avoid. The tradeoff is that joins are
    explicit, which is arguably clearer at this size.
    """
    stmt = (select(Section, CurrentSeats)
            .outerjoin(CurrentSeats, and_(CurrentSeats.crn == Section.crn,
                                          CurrentSeats.term == Section.term))
            .where(Section.course_id == course_id, Section.term == term)
            .order_by(Section.type_of_class, Section.section_no))
    return session.execute(stmt).all()


def get_instructors(session, course_id, term):
    """Instructor names for a course in a term. Term-scoped: unfiltered you'd
    list next year's staff alongside this year's."""
    stmt = (select(CourseInstructor.name)
            .where(CourseInstructor.course_id == course_id,
                   CourseInstructor.term == term)
            .order_by(CourseInstructor.name))
    return list(session.execute(stmt).scalars().all())


def get_requirements(session, course_id):
    """
    What this course requires, each marked as resolvable or external.

    LEFT JOIN, not inner: 294 of 2,778 requirement codes name courses outside
    the catalogue -- retired courses, other institutions, CEGEP objectives. An
    inner join would silently drop them, so a course would appear to have fewer
    prerequisites than the calendar says. `title` comes back NULL for those and
    the caller renders them as plain text rather than a link.
    """
    stmt = (select(CoursePrerequisite.required_code, CoursePrerequisite.kind,
                   Course.id, Course.title)
            .outerjoin(Course, Course.catalogue_id == CoursePrerequisite.required_code)
            .where(CoursePrerequisite.course_id == course_id)
            .order_by(CoursePrerequisite.kind, CoursePrerequisite.required_code))
    return session.execute(stmt).all()


def get_unlocks(session, course_id, limit=24):
    """
    Courses that list this one as a requirement -- the catalogue's `leadingTo`,
    derived rather than stored.

    Matches on catalogue_id ("COMP250") because that is the shape the edge table
    holds, not the dashed canonical id. Uses ix_prereq_reverse.
    """
    catalogue_id = select(Course.catalogue_id).where(
        Course.id == course_id).scalar_subquery()

    stmt = (select(Course.id, Course.title, Course.avg_rating, Course.review_count)
            .join(CoursePrerequisite,
                  CoursePrerequisite.course_id == Course.id)
            .where(CoursePrerequisite.required_code == catalogue_id,
                   CoursePrerequisite.kind == "prerequisite")
            .order_by(Course.review_count.desc().nulls_last())
            .limit(limit))
    return session.execute(stmt).all()


def filter_options(session, term):
    """
    Distinct values for each filterable dimension, scoped to courses offered in
    `term`.

    Scoping matters: the catalogue has 249 subjects but only ~180 are offered in
    a given Fall. Offering the other 69 in a dropdown guarantees empty results.
    """
    offered = _section_exists(term)

    def course_column(column):
        stmt = (select(column).where(offered, column.isnot(None))
                .distinct().order_by(column))
        return [v for v in session.execute(stmt).scalars().all()]

    # Ordered by how many student reviews the subject has drawn, NOT by how
    # many courses it lists. Course count ranks DENT, MIME, MUIN and EXTL at the
    # top -- dentistry, mechanical-engineering design, music instruction and
    # external credit -- which is a true fact about the catalogue and useless as
    # a way in. Reviews rank MATH, MGCR, PSYC, COMP, which is what people
    # actually typed into the search box today.
    subjects_stmt = (select(Course.subject,
                            func.count(),
                            func.coalesce(func.sum(Course.review_count), 0)
                                .label("reviews"))
                     .where(offered, func.left(Course.code, 1) <= "4")
                     .group_by(Course.subject)
                     .order_by(func.coalesce(func.sum(Course.review_count), 0).desc()))

    section_column = lambda column: [
        v for v in session.execute(
            select(column).where(Section.term == term, column.isnot(None))
            .distinct().order_by(column)).scalars().all()]

    # Distinct credit values, commonest first -- 3 alone covers ~2,400 courses,
    # so the UI wants them ordered by popularity, not numerically.
    credits_stmt = (select(Course.credits_min, func.count())
                    .where(offered, Course.credits_min.isnot(None))
                    .group_by(Course.credits_min)
                    .order_by(func.count().desc()))

    # Level is the first character of `code`; only the numeric ones are levels.
    #
    # `level` is built ONCE and reused. Calling func.left() twice produces two
    # separate bind parameters -- `left(code, %(left_2)s)` in the SELECT and
    # `left(code, %(left_3)s)` in the ORDER BY -- and under SELECT DISTINCT
    # Postgres compares those expressions textually, decides they differ, and
    # raises "ORDER BY expressions must appear in select list".
    level = func.left(Course.code, 1)
    levels_stmt = (select(level)
                   .where(offered, level.between("1", "9"))
                   .distinct().order_by(level))

    return {
        "term": term,
        "faculties": course_column(Course.faculty),
        "departments": course_column(Course.department),
        "subjects": [{"code": code, "count": n}
                     for code, n, _reviews in session.execute(subjects_stmt).all()],
        "campuses": section_column(Section.campus),
        "class_types": section_column(Section.type_of_class),
        "credit_options": [float(c) for c, _ in
                           session.execute(credits_stmt).all()],
        "levels": [int(d) * 100 for d in
                   session.execute(levels_stmt).scalars().all()],
    }


def count_matching(session, **filters):

    """
    How many courses match, ignoring limit and ordering.

    Lets the UI say "showing 50 of 312" and, more usefully, "2,719 unreviewed
    courses hidden" -- the reviewed-only boundary should be visible rather than
    mysterious.
    """
    stmt = select(func.count()).select_from(Course).where(
        *_filter_clauses(**filters))
    return session.execute(stmt).scalar()