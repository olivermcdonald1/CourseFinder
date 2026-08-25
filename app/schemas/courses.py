from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.sections import MovementResponse

class CourseFilters(BaseModel):
    subject: str | None = None
    course_id: str | None = None
    title: str | None = None
    keywords: str | None = None

    credits: float | None = Field(None, ge=0, le=30)
    # Repeatable: ?levels=200&levels=300. Kept alongside min/max rather than
    # replacing them, so existing links and the API's range semantics still
    # work; when both are given, the explicit set wins.
    credits_any: list[float] | None = Field(None, max_length=12)
    levels: list[int] | None = Field(None, max_length=9)
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
    # Minutes from midnight. 600 = "nothing before 10am".
    earliest_start: int | None = Field(None, ge=0, le=1439)
    has_history: bool = False

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
    department: str | None
    avg_rating: float | None
    avg_difficulty: float | None
    review_count: int | None

    # Seat rollup across the course's sections this term, grafted on by the
    # service from search_page()'s extra columns. None means the course has
    # sections but no observation yet -- which is not the same as zero seats.
    n_sections: int | None = None
    open_seats: int | None = None
    waitlist_count: int | None = None
    has_history: bool = False
    # When the newest of this course's sections was last swept, so the UI can
    # say how old the seat number is instead of implying it is live to the
    # second.
    observed_at: datetime | None = None

    # Seats gained (+) or lost (-) across the course's sections since we first
    # saw it, and over how many days. seats_per_day is the actionable form: a
    # student can turn "-4.5 a day" into "gone by Thursday"; a raw total can't.
    seats_delta: int | None = None
    trend_days: int | None = None
    seats_per_day: float | None = None

    # avg_rating is the plain mean of the reviews. weighted_rating pulls it
    # toward the catalogue average in proportion to how few reviews there are,
    # so five perfect reviews cannot outrank nine hundred good ones. It is what
    # the "Best rated" ordering uses, and therefore what the list should show.
    weighted_rating: float | None = None

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
    # "Tue Thu 11:35-12:55 · MCMED 522" -- assembled by the service, because
    # `meetings` holds VSB's raw day integers and minutes-from-midnight, which
    # no interface should ask a student to decode.
    schedule_text: str | None = None
    rooms: str | None = None
    open_seats: int | None
    waitlist_count: int | None
    waitlist_seats: int | None
    is_full: bool | None
    observed_at: datetime | None


class Requirement(BaseModel):
    """
    One requirement edge. `course_id` is None when the code names something
    outside the catalogue (a retired course, a CEGEP objective) -- the UI shows
    those as plain text rather than a link.
    """

    code: str
    kind: str
    course_id: str | None
    title: str | None


class Unlocks(BaseModel):
    course_id: str
    title: str
    avg_rating: float | None
    review_count: int | None


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

    # Requirement prose is authoritative; the tree preserves AND/OR that the
    # flat edge list destroys; the edges are for traversal.
    prerequisites_text: str | None = None
    corequisites_text: str | None = None
    restrictions_text: str | None = None
    logical_prerequisites: dict | None = None

    # Filled in by the service, not read off the Course row.
    term: str = ""
    instructors: list[str] = []
    sections: list[SectionSummary] = []
    requirements: list[Requirement] = []
    unlocks: list[Unlocks] = []

    # Seat history for the first section, inlined rather than left to a second
    # request. The client needed a CRN from this payload before it could ask
    # for movement, so the two calls were serial across a ~300ms link -- while
    # here the same pair of queries are ~1ms apart. Null when the course has no
    # sections in this term, or none has been swept twice yet.
    movement: MovementResponse | None = None


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