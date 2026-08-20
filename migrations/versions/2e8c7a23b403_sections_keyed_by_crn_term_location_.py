"""sections keyed by (crn,term), location into meetings, seat_watch term

Revision ID: 2e8c7a23b403
Revises: b2841debb69e
Create Date: 2026-08-17 14:39:44.999011

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2e8c7a23b403'
down_revision: Union[str, Sequence[str], None] = 'b2841debb69e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Hand-written. Autogenerate does NOT detect primary key changes, so it
    # emitted only the column and FK edits -- and its FK referenced
    # sections(crn, term), which cannot exist until that pair is unique.
    op.drop_constraint(op.f('seat_watch_crn_fkey'), 'seat_watch', type_='foreignkey')

    # CRNs repeat across terms, so the section identity is the pair.
    op.drop_constraint(op.f('sections_pkey'), 'sections', type_='primary')
    op.create_primary_key('sections_pkey', 'sections', ['crn', 'term'])

    # An observation points at a section, so it needs the same pair.
    op.add_column('seat_watch', sa.Column('term', sa.String(), nullable=False))
    op.drop_constraint(op.f('seat_watch_pkey'), 'seat_watch', type_='primary')
    op.create_primary_key('seat_watch_pkey', 'seat_watch',
                          ['crn', 'term', 'observed_at'])
    op.create_foreign_key('seat_watch_crn_term_fkey', 'seat_watch', 'sections',
                          ['crn', 'term'], ['crn', 'term'])

    # Rooms vary per meeting, so location moved inside sections.meetings.
    op.drop_column('sections', 'location')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('sections', sa.Column('location', sa.VARCHAR(),
                                        autoincrement=False, nullable=True))

    op.drop_constraint('seat_watch_crn_term_fkey', 'seat_watch', type_='foreignkey')
    op.drop_constraint(op.f('seat_watch_pkey'), 'seat_watch', type_='primary')
    op.create_primary_key('seat_watch_pkey', 'seat_watch', ['crn', 'observed_at'])
    op.drop_column('seat_watch', 'term')

    op.drop_constraint(op.f('sections_pkey'), 'sections', type_='primary')
    op.create_primary_key('sections_pkey', 'sections', ['crn'])

    op.create_foreign_key(op.f('seat_watch_crn_fkey'), 'seat_watch', 'sections',
                          ['crn'], ['crn'])
