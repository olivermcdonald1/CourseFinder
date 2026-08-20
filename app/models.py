from decimal import Decimal
from sqlalchemy import (Numeric, ForeignKey, ForeignKeyConstraint, DateTime,
                        Date, Index, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import date, datetime
class Base(DeclarativeBase):
    pass

class Course(Base):
    __tablename__ = "courses"
    id: Mapped[str] = mapped_column(primary_key=True)   # "COMP-250"
    catalogue_id: Mapped[str]                            # "COMP250" — their _id
    subject: Mapped[str]
    code: Mapped[str]
    title: Mapped[str]
    description: Mapped[str | None]
    # 11 catalogue values aren't numbers -- ".66", "0-3", "0-15" are credit
    # RANGES for variable-credit courses. Keep the raw string as the source of
    # truth and expose the parsed bounds for querying. A fixed-credit course
    # has min == max.
    credits_text: Mapped[str | None]
    credits_min: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    credits_max: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    faculty: Mapped[str | None]
    department: Mapped[str | None]
    url: Mapped[str | None]
    avg_rating: Mapped[float | None]
    avg_difficulty: Mapped[float | None]
    review_count: Mapped[int | None]

class CourseInstructor(Base):
    """
    Who teaches a course in a given term.

    This is the finest grain either source actually supports. Neither feed says
    which instructor teaches which SECTION: the catalogue's schedule blocks have
    no instructor key, and VSB's `teacher` was empty on every section observed.
    Nor is it inferable -- 1,141 courses list more instructors than they have
    lecture sections (BIOC212: 5 instructors, 2 sections).

    If VSB starts publishing `teacher` closer to registration, add a separate
    section_instructors(crn, name) table and let queries prefer it, falling back
    to this one. Don't add an instructor column to Section: it would imply one
    instructor per section, which is false even in reality for co-taught courses.
    """
    __tablename__ = "course_instructors"
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), primary_key=True)
    term: Mapped[str] = mapped_column(primary_key=True)      # "Fall 2026"
    name: Mapped[str] = mapped_column(primary_key=True)

class Section(Base):
    """
    A schedulable block with its own CRN.

    Fed by BOTH sources: the catalogue describes the section (campus, rooms,
    times), VSB reports its live state (status, seats). Where both have an
    opinion, live wins -- COALESCE(vsb, catalogue) -- because rooms and times
    get reassigned after the catalogue snapshot is taken.

    PRIMARY KEY IS (crn, term), NOT crn ALONE
      McGill reuses CRNs between terms: of 8,163 distinct CRNs in the catalogue,
      6,240 appear in more than one term. CRN 516 is AAAA100 in Fall 2026 and
      WCOM642 in Summer 2026. No (crn, term) pair is ever shared by two courses,
      so the pair is the real identity.

    LOCATION LIVES ON THE MEETING, NOT THE SECTION
      224 sections meet in more than one room -- the catalogue's "block" is a
      (section, meeting-pattern) record, so a section can be in ARMST 265 on
      Tuesday and elsewhere on Thursday. A single location column would have to
      silently discard one of them.
    """
    __tablename__ = "sections"
    crn:Mapped[str] = mapped_column(primary_key=True)
    term:Mapped[str] = mapped_column(primary_key=True)        # "202609"
    course_id:Mapped[str] = mapped_column(ForeignKey("courses.id"))
    section_no:Mapped[str|None]
    type_of_class:Mapped[str|None]
    campus:Mapped[str|None]
    meetings:Mapped[list[dict]|None] = mapped_column(JSONB)   # day, start/end mins, location

class CurrentSeats(Base):
    """
    The latest thing VSB said about a section. One row per section, forever.

    UPSERTed on every sweep, so this table never grows past the number of
    sections (~7.5k for a term). Everything the search needs -- "has seats",
    "waitlist length" -- reads from here, so no query has to find a maximum
    observed_at across a growing history table.
    """
    __tablename__ = "current_seats"
    __table_args__ = (
        ForeignKeyConstraint(["crn", "term"], ["sections.crn", "sections.term"]),
    )
    crn:Mapped[str] = mapped_column(primary_key=True)
    term:Mapped[str] = mapped_column(primary_key=True)
    observed_at:Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_seats:Mapped[int|None]
    waitlist_count:Mapped[int|None]
    waitlist_seats:Mapped[int|None]
    is_full:Mapped[bool|None]
    max_enrol:Mapped[int|None]
    non_reserved:Mapped[int|None]
    status:Mapped[str|None]

class SeatDaily(Base):
    """
    One snapshot per section per day -- the history that makes movement
    calculable ("waitlist moved 12 spots this week").

    KEYED ON day, NOT observed_at
      Sweep frequency and snapshot frequency are deliberately decoupled. The
      collector can run every 20 minutes to keep current_seats fresh; each run
      UPSERTs the same (crn, term, day) row, so the day ends holding its final
      state and the table grows by ~7.5k rows/day regardless of sweep rate.
      Measured at 155 bytes/row, a full term is ~140MB.

      Intraday resolution was considered and rejected: "did it move today" is
      answered by current_seats, and "how fast is it moving" needs days.
    """
    __tablename__ = "seat_daily"
    __table_args__ = (
        ForeignKeyConstraint(["crn", "term"], ["sections.crn", "sections.term"]),
        # Declared here as well as in the migration that created it. An index
        # that exists only in a migration looks like drift to autogenerate,
        # which then proposes dropping it on every future revision.
        Index("ix_seat_daily_lookup", "crn", "term", text("day DESC")),
    )
    crn:Mapped[str] = mapped_column(primary_key=True)
    term:Mapped[str] = mapped_column(primary_key=True)
    day:Mapped[date] = mapped_column(Date, primary_key=True)
    observed_at:Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_seats:Mapped[int|None]
    waitlist_count:Mapped[int|None]
    waitlist_seats:Mapped[int|None]
    is_full:Mapped[bool|None]
    max_enrol:Mapped[int|None]
