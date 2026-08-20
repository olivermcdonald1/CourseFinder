from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class CourseFilters(BaseModel):
    subject: str | None = None
    course_id: str | None = None
    title: str | None = None
    keywords: str | None = None

    credits: float | None = Field(None, ge=0, le=30)
    min_level: int | None = Field(None, ge=100, le=900)
    max_level: int | None = Field(None, ge=100, le=900)
    undergrad_only: bool = True

    faculty: str | None = None
    department: str | None = None
    instructor: str | None = None

    max_difficulty: float | None = Field(None, ge=1, le=5)
    min_rating: float | None = Field(None, ge=1, le=5)
    min_reviews: int | None = Field(None, ge=0)
    reviewed_only: bool | None = None

    term: str = "202609"
    campus: Literal["Downtown", "Macdonald", "Distance", "Off-campus"] | None = None
    has_open_seats: bool = False
    max_waitlist: int | None = Field(None, ge=0)

    sort_by: Literal["rating", "easiest", "popular", "seats"] | None = None
    limit: int = Field(50, ge=1, le=100)
    offset: int = Field(0, ge=0)

class CourseSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    subject: str
    code: str
    title: str
    credits_text: str | None
    faculty: str | None
    avg_rating: float | None
    avg_difficulty: float | None
    review_count: int | None

class SearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    hidden_unreviewed: int
    courses: list[CourseSummary]


class SectionSummary(BaseModel):
    """
    A section plus its live seat state.

    Assembled from a Section row LEFT JOINed to current_seats, so the seat
    fields are None for a section we haven't observed yet -- which is different
    from "full" and shouldn't be rendered as zero.
    """

    crn: str
    section_no: str | None
    type_of_class: str | None
    campus: str | None
    meetings: list[dict] | None
    open_seats: int | None
    waitlist_count: int | None
    waitlist_seats: int | None
    is_full: bool | None
    observed_at: datetime | None


class CourseDetail(BaseModel):
    """
    One course, everything about it.

    credits_min/max are declared float, not Decimal: Pydantic v2 serializes
    Decimal as a JSON *string* ("3.00"), which would make the frontend parse
    numbers out of strings for no reason.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    subject: str
    code: str
    title: str
    description: str | None
    credits_text: str | None
    credits_min: float | None
    credits_max: float | None
    faculty: str | None
    department: str | None
    url: str | None
    avg_rating: float | None
    avg_difficulty: float | None
    review_count: int | None

    # Filled in by the service, not read off the Course row.
    term: str = ""
    instructors: list[str] = []
    sections: list[SectionSummary] = []


class SubjectOption(BaseModel):
    code: str
    count: int


class FilterOptions(BaseModel):
    """
    Option lists for the filter chips and dropdowns.

    Every list is scoped to courses OFFERED in the requested term. A dropdown
    offering 249 subjects when only ~180 are offered this Fall produces empty
    result sets and looks broken.
    """

    term: str
    faculties: list[str]
    departments: list[str]
    subjects: list[SubjectOption]
    campuses: list[str]
    class_types: list[str]
    credit_options: list[float]
    levels: list[int]