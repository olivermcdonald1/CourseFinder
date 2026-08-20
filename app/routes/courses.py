from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.dependencies.db import SessionDep
from app.schemas.courses import (CourseDetail, CourseFilters, FilterOptions,
                                 SearchResponse)
from app.search import DEFAULT_TERM
from app.services import courses as course_service

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("", response_model=SearchResponse)
def search(filters: Annotated[CourseFilters, Query()], session: SessionDep):
    """Search courses. Every filter is optional."""
    return course_service.search(session, filters)


# Declared BEFORE /{course_id} on purpose: routes match in registration order,
# so "/courses/filters" would otherwise be captured as course_id="filters".
@router.get("/filters", response_model=FilterOptions, tags=["meta"])
def options(session: SessionDep,
            term: Annotated[str, Query()] = DEFAULT_TERM):
    """Option lists for the filter chips, scoped to courses offered in `term`."""
    return course_service.options(session, term)


@router.get("/{course_id}", response_model=CourseDetail)
def detail(session: SessionDep,
           course_id: Annotated[str, Path(examples=["COMP-250"])],
           term: Annotated[str, Query()] = DEFAULT_TERM):
    """One course with its sections, live seat counts, and instructors."""
    course = course_service.detail(session, course_id, term)
    if course is None:
        # Returning None here would serialise as a 200 with a null body, which
        # tells the client "this course exists and is empty".
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"No course {course_id!r}")
    return course
