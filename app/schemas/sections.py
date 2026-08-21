from datetime import date, datetime

from pydantic import BaseModel


class SeatState(BaseModel):
    """What VSB last said about a section."""

    open_seats: int | None
    waitlist_count: int | None
    waitlist_seats: int | None
    is_full: bool | None
    observed_at: datetime


class SeatDeltas(BaseModel):
    """
    Movement over 1/3/7 days, derived from daily snapshots.

    All fields are nullable and will be null until there is history to compare
    against. That is correct output, not a bug -- a section swept once has no
    earlier snapshot.
    """

    wl_delta_1d: int | None
    wl_delta_3d: int | None
    wl_delta_7d: int | None
    seats_delta_7d: int | None
    # The "7-day" window is really "nearest snapshot at or before 7 days ago",
    # because sweeps get missed. This says how wide the window actually was so
    # the UI can report "12 spots over 8 days" honestly.
    days_span_7d: int | None
    wl_cleared_per_day: float | None


class ObservedWindow(BaseModel):
    """
    Movement over the span actually observed, rather than a fixed 7 days.

    Always populated once two snapshots exist, and it reports its own `days` so
    the UI can say "3 seats over 2 days" instead of implying a week.
    """

    from_day: date
    to_day: date
    days: int
    open_delta: int | None
    waitlist_delta: int | None
    open_per_day: float | None


class SeatDay(BaseModel):
    day: date
    open_seats: int | None
    waitlist_count: int | None


class MovementResponse(BaseModel):
    crn: str
    term: str
    course_id: str
    section_no: str | None
    type_of_class: str | None
    current: SeatState | None
    deltas: SeatDeltas | None
    observed: ObservedWindow | None
    history: list[SeatDay]
