"""
Seat movement for one section: current state, deltas, and the daily series.

Raw SQL rather than the ORM, deliberately. section_movement is a view built on
three LEFT JOIN LATERAL subqueries; expressing that through select() would be
considerably harder to read than the SQL itself, and the view already exists as
the tested artifact. `text()` with bound parameters is still parameterised --
:crn is never interpolated into the string.
"""

from sqlalchemy import text

MOVEMENT_SQL = text("""
    SELECT open_seats, waitlist_count, waitlist_seats, is_full, observed_at,
           wl_delta_1d, wl_delta_3d, wl_delta_7d,
           seats_delta_7d, days_span_7d, wl_cleared_per_day
    FROM section_movement
    WHERE crn = :crn AND term = :term
""")

HISTORY_SQL = text("""
    SELECT day, open_seats, waitlist_count
    FROM seat_daily
    WHERE crn = :crn AND term = :term
    ORDER BY day
""")

SECTION_SQL = text("""
    SELECT course_id, section_no, type_of_class
    FROM sections
    WHERE crn = :crn AND term = :term
""")


def _observed_window(history):
    """
    First snapshot vs latest, with the real span between them.

    Returns None with fewer than two snapshots -- one observation is a reading,
    not a trend, and inventing a zero delta would claim we'd watched something
    hold steady when we had only looked once.

    `per_day` is signed: negative means seats are disappearing. Rate is the
    number a student can act on ("about 5 a day, so it's gone by Friday");
    the raw delta alone doesn't say how fast.
    """
    if len(history) < 2:
        return None

    first, last = history[0], history[-1]
    days = (last["day"] - first["day"]).days
    if days <= 0:
        return None

    def delta(field):
        a, b = first[field], last[field]
        return None if a is None or b is None else b - a

    seats = delta("open_seats")
    return {
        "from_day": first["day"],
        "to_day": last["day"],
        "days": days,
        "open_delta": seats,
        "waitlist_delta": delta("waitlist_count"),
        "open_per_day": None if seats is None else round(seats / days, 2),
    }


def get_movement(session, crn, term):
    """
    -> dict, or None if no such (crn, term) section exists.

    Three distinct "empty" cases, and they mean different things:
      - no section          -> None, so the route can 404
      - section, no sweep   -> current is None; the section exists, we just
                               haven't observed it
      - swept once          -> current is populated, deltas are None because
                               there is no earlier snapshot to compare against
    Collapsing these into one would make "we don't know" indistinguishable from
    "there are no seats".
    """
    params = {"crn": crn, "term": term}

    section = session.execute(SECTION_SQL, params).mappings().first()
    if section is None:
        return None

    movement = session.execute(MOVEMENT_SQL, params).mappings().first()
    history = session.execute(HISTORY_SQL, params).mappings().all()

    return {
        "crn": crn,
        "term": term,
        # Movement over the window we ACTUALLY have, not a fixed 7 days.
        #
        # The view's 1/3/7-day columns look for a snapshot at or before a fixed
        # offset, so a section first seen two days ago reports NULL for all of
        # them -- it says "tracked" and then shows nothing, which is the worst
        # of both. This is computed from the series above, so it is populated
        # the moment a second snapshot exists, and it always states its own
        # span rather than implying a week.
        "observed": _observed_window(history),
        "course_id": section["course_id"],
        "section_no": section["section_no"],
        "type_of_class": section["type_of_class"],
        "current": None if movement is None else {
            "open_seats": movement["open_seats"],
            "waitlist_count": movement["waitlist_count"],
            "waitlist_seats": movement["waitlist_seats"],
            "is_full": movement["is_full"],
            "observed_at": movement["observed_at"],
        },
        "deltas": None if movement is None else {
            "wl_delta_1d": movement["wl_delta_1d"],
            "wl_delta_3d": movement["wl_delta_3d"],
            "wl_delta_7d": movement["wl_delta_7d"],
            "seats_delta_7d": movement["seats_delta_7d"],
            # Reported alongside the 7-day delta because sweeps get missed: the
            # view finds the nearest snapshot at or BEFORE 7 days ago, so a
            # "7-day" delta may actually span 8. The UI should say which.
            "days_span_7d": movement["days_span_7d"],
            "wl_cleared_per_day": (
                float(movement["wl_cleared_per_day"])
                if movement["wl_cleared_per_day"] is not None else None),
        },
        "history": [dict(row) for row in history],
    }
