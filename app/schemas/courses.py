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