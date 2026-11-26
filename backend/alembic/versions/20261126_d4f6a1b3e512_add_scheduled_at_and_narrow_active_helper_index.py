"""add scheduled_at to bookings and narrow uq_bookings_active_helper to in-flight statuses

Revision ID: d4f6a1b3e512
Revises: c3e5f0a2d409
Create Date: 2026-11-26 19:30:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f6a1b3e512"
down_revision: Union[str, Sequence[str], None] = "c3e5f0a2d409"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_bookings_scheduled_at"), "bookings", ["scheduled_at"], unique=False)
    op.drop_index("uq_bookings_active_helper", table_name="bookings")
    op.create_index(
        "uq_bookings_active_helper",
        "bookings",
        ["helper_id"],
        unique=True,
        postgresql_where=sa.text("helper_id IS NOT NULL AND status IN ('en_route', 'in_progress')"),
    )


def downgrade() -> None:
    op.drop_index("uq_bookings_active_helper", table_name="bookings")
    op.create_index(
        "uq_bookings_active_helper",
        "bookings",
        ["helper_id"],
        unique=True,
        postgresql_where=sa.text(
            "helper_id IS NOT NULL AND status IN ('accepted', 'en_route', 'in_progress')"
        ),
    )
    op.drop_index(op.f("ix_bookings_scheduled_at"), table_name="bookings")
    op.drop_column("bookings", "scheduled_at")
