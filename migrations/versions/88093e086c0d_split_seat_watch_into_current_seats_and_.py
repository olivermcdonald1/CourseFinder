"""split seat_watch into current_seats and seat_daily

Replaces the append-every-observation table with two: a never-growing table of
the latest state per section, and one snapshot per section per day. Existing
seat_watch rows are carried over, not dropped -- they're the only history that
exists and can't be re-fetched.

Also adds the section_movement view, which derives 1/3/7-day deltas from the
snapshots rather than storing them as columns. A view can be redefined for any
window later; a stored delta column can't be backfilled.

Revision ID: 88093e086c0d
Revises: 2e8c7a23b403
Create Date: 2026-08-18 13:22:37.821526

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '88093e086c0d'
down_revision: Union[str, Sequence[str], None] = '2e8c7a23b403'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# "Most recent snapshot at or before N days ago", not "the snapshot exactly N
# days ago" -- sweeps fail and laptops sleep, so an exact-date join silently
# reports no movement. days_span comes back with each delta so the UI can say
# "12 spots over 6 days" rather than implying the window was exact.
_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT d.waitlist_count, d.open_seats, d.day
        FROM seat_daily d
        WHERE d.crn = c.crn AND d.term = c.term
          AND d.day <= CURRENT_DATE - {days}
        ORDER BY d.day DESC
        LIMIT 1
    ) d{days} ON true
"""

SECTION_MOVEMENT = f"""
CREATE VIEW section_movement AS
SELECT c.crn, c.term, c.observed_at,
       c.open_seats, c.waitlist_count, c.waitlist_seats, c.is_full,
       c.waitlist_count - d1.waitlist_count  AS wl_delta_1d,
       c.waitlist_count - d3.waitlist_count  AS wl_delta_3d,
       c.waitlist_count - d7.waitlist_count  AS wl_delta_7d,
       c.open_seats    - d7.open_seats       AS seats_delta_7d,
       CURRENT_DATE - d7.day                 AS days_span_7d,
       -- Spots cleared per day over the last week. This is the number that
       -- turns "you are 40th in line" into "about 9 days at this rate".
       CASE
           WHEN d7.day IS NULL OR CURRENT_DATE = d7.day THEN NULL
           ELSE (d7.waitlist_count - c.waitlist_count)::numeric
                / (CURRENT_DATE - d7.day)
       END AS wl_cleared_per_day
FROM current_seats c
{_LATERAL.format(days=1)}
{_LATERAL.format(days=3)}
{_LATERAL.format(days=7)}
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "current_seats",
        sa.Column("crn", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_seats", sa.Integer(), nullable=True),
        sa.Column("waitlist_count", sa.Integer(), nullable=True),
        sa.Column("waitlist_seats", sa.Integer(), nullable=True),
        sa.Column("is_full", sa.Boolean(), nullable=True),
        sa.Column("max_enrol", sa.Integer(), nullable=True),
        sa.Column("non_reserved", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("crn", "term"),
        sa.ForeignKeyConstraint(["crn", "term"], ["sections.crn", "sections.term"]),
    )

    op.create_table(
        "seat_daily",
        sa.Column("crn", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_seats", sa.Integer(), nullable=True),
        sa.Column("waitlist_count", sa.Integer(), nullable=True),
        sa.Column("waitlist_seats", sa.Integer(), nullable=True),
        sa.Column("is_full", sa.Boolean(), nullable=True),
        sa.Column("max_enrol", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("crn", "term", "day"),
        sa.ForeignKeyConstraint(["crn", "term"], ["sections.crn", "sections.term"]),
    )

    # Serves the LATERAL lookups in section_movement: seek straight to the
    # newest row at or before a date, per section.
    op.create_index("ix_seat_daily_lookup", "seat_daily",
                    ["crn", "term", sa.text("day DESC")])

    # Carry the existing observations over. DISTINCT ON keeps the last
    # observation of each day, matching the UPSERT semantics the loader uses.
    # sw.* qualifiers are load-bearing: `observed_at::date` inherits the name
    # `observed_at`, so an unqualified ORDER BY observed_at is ambiguous
    # between the cast output column and the raw one.
    op.execute("""
        INSERT INTO seat_daily
            (crn, term, day, observed_at, open_seats, waitlist_count,
             waitlist_seats, is_full, max_enrol)
        SELECT DISTINCT ON (sw.crn, sw.term, sw.observed_at::date)
            sw.crn, sw.term, sw.observed_at::date AS day, sw.observed_at,
            sw.open_seats, sw.waitlist_count, sw.waitlist_seats,
            sw.is_full, sw.max_enrol
        FROM seat_watch sw
        ORDER BY sw.crn, sw.term, sw.observed_at::date, sw.observed_at DESC
    """)

    op.execute("""
        INSERT INTO current_seats
            (crn, term, observed_at, open_seats, waitlist_count, waitlist_seats,
             is_full, max_enrol, non_reserved, status)
        SELECT DISTINCT ON (sw.crn, sw.term)
            sw.crn, sw.term, sw.observed_at, sw.open_seats, sw.waitlist_count,
            sw.waitlist_seats, sw.is_full, sw.max_enrol, sw.non_reserved, sw.status
        FROM seat_watch sw
        ORDER BY sw.crn, sw.term, sw.observed_at DESC
    """)

    op.drop_table("seat_watch")
    op.execute(SECTION_MOVEMENT)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW IF EXISTS section_movement")

    op.create_table(
        "seat_watch",
        sa.Column("crn", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_seats", sa.Integer(), nullable=True),
        sa.Column("waitlist_count", sa.Integer(), nullable=True),
        sa.Column("waitlist_seats", sa.Integer(), nullable=True),
        sa.Column("is_full", sa.Boolean(), nullable=True),
        sa.Column("max_enrol", sa.Integer(), nullable=True),
        sa.Column("non_reserved", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("crn", "term", "observed_at"),
        sa.ForeignKeyConstraint(["crn", "term"], ["sections.crn", "sections.term"]),
    )

    # Only the daily grain survives a round trip -- intraday observations were
    # already collapsed on the way up and cannot be reconstructed.
    op.execute("""
        INSERT INTO seat_watch
            (crn, term, observed_at, open_seats, waitlist_count, waitlist_seats,
             is_full, max_enrol)
        SELECT crn, term, observed_at, open_seats, waitlist_count,
               waitlist_seats, is_full, max_enrol
        FROM seat_daily
    """)

    op.drop_index("ix_seat_daily_lookup", table_name="seat_daily")
    op.drop_table("seat_daily")
    op.drop_table("current_seats")
