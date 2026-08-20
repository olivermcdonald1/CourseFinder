"""
Search orchestration.

The only file here with reasoning in it: one search request becomes two or three
queries, and this decides which. Not HTTP (that's routes/), not SQL (that's
search.py).
"""

from app.schemas.courses import CourseSummary, SearchResponse
from app.search import count_matching, search_courses

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
    courses = search_courses(session, limit=limit, offset=offset,
                             sort_by=sort_by, **kwargs)

    return SearchResponse(
        total=total,
        limit=limit,
        offset=offset,
        hidden_unreviewed=_hidden_count(session, kwargs, total),
        # Convert INSIDE the session scope. A Course handed back raw would raise
        # DetachedInstanceError when serialization touched a lazy attribute
        # after the dependency closed the session.
        courses=[CourseSummary.model_validate(c) for c in courses],
    )


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
