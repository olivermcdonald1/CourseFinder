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

from sqlalchemy import and_, case, exists, func, or_, select

from app.models import Course, CourseInstructor, CurrentSeats, Section

DEFAULT_TERM = "202609"          # Fall 2026
DEFAULT_LIMIT = 50

UNDERGRAD_MAX_LEVEL = 400

# Weight of the prior in the damped rating: how many "average" reviews a course
# is credited with before its own reviews start to count. Roughly the median
# review count. Higher = more sceptical of small samples.
PRIOR_WEIGHT = 20

SORT_OPTIONS = ("rating", "easiest", "popular", "seats")


def _section_exists(term, *, campus=None, class_type=None,
                    open_seats=False, max_waitlist=None):
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
        pattern = f"%{keywords}%"
        clauses.append(or_(Course.title.ilike(pattern),
                           Course.description.ilike(pattern)))

    if credits is not None:
        # min == max for fixed-credit courses, so these two predicates cover
        # both them and variable-credit ranges like "0-15".
        clauses.append(Course.credits_min <= credits)
        clauses.append(Course.credits_max >= credits)
    if undergrad_only and max_level is None:
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

    # One EXISTS for every section-level condition, so they describe the same
    # section rather than three unrelated ones.
    needs_section = (campus is not None or class_type is not None
                     or has_open_seats or max_waitlist is not None)
    if needs_section:
        clauses.append(_section_exists(
            term, campus=campus, class_type=class_type,
            open_seats=has_open_seats, max_waitlist=max_waitlist))
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

    # Tier 1 -- relevance. Only meaningful for a keyword search: a title hit
    # beats a description mention, so "climate" stops surfacing "The Italian
    # Renaissance". When a subject is named, every match is equally relevant
    # and this tier is silent.
    keywords = filters.get("keywords")
    if keywords is not None:
        order.append(
            case((Course.title.ilike(f"%{keywords}%"), 0), else_=1).asc())

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

    subjects_stmt = (select(Course.subject, func.count())
                     .where(offered)
                     .group_by(Course.subject)
                     .order_by(func.count().desc()))

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
        "subjects": [{"code": s, "count": n}
                     for s, n in session.execute(subjects_stmt).all()],
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