"""add booking lifecycle fields and booking_offers table

Revision ID: c3e5f0a2d409
Revises: b2d4e9f1c307
Create Date: 2026-11-22 22:18:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3e5f0a2d409"
down_revision: Union[str, Sequence[str], None] = "b2d4e9f1c307"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

URGENCY_VALUES = ("standard", "urgent")
MATCHING_MODE_VALUES = ("auto", "manual")
OFFER_STATUS_VALUES = ("pending", "accepted", "declined", "superseded", "expired")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*URGENCY_VALUES, name="booking_urgency_enum").create(bind, checkfirst=True)
    postgresql.ENUM(*MATCHING_MODE_VALUES, name="booking_matching_mode_enum").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*OFFER_STATUS_VALUES, name="booking_offer_status_enum").create(
        bind, checkfirst=True
    )

    op.add_column(
        "bookings",
        sa.Column(
            "urgency",
            postgresql.ENUM(*URGENCY_VALUES, name="booking_urgency_enum", create_type=False),
            server_default=sa.text("'standard'"),
            nullable=False,
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "duration_hours",
            sa.Float(),
            server_default=sa.text("1.0"),
            nullable=False,
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "matching_mode",
            postgresql.ENUM(
                *MATCHING_MODE_VALUES, name="booking_matching_mode_enum", create_type=False
            ),
            server_default=sa.text("'auto'"),
            nullable=False,
        ),
    )
    op.add_column("bookings", sa.Column("min_price", sa.Float(), nullable=True))
    op.add_column("bookings", sa.Column("max_price", sa.Float(), nullable=True))
    op.add_column("bookings", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("cancelled_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_bookings_cancelled_by_users",
        "bookings",
        "users",
        ["cancelled_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_bookings_duration_hours_range",
        "bookings",
        "duration_hours > 0 AND duration_hours <= 24",
    )
    op.create_index(
        "uq_bookings_active_helper",
        "bookings",
        ["helper_id"],
        unique=True,
        postgresql_where=sa.text(
            "helper_id IS NOT NULL AND status IN ('accepted', 'en_route', 'in_progress')"
        ),
    )

    op.create_table(
        "booking_offers",
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("helper_id", sa.UUID(), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *OFFER_STATUS_VALUES, name="booking_offer_status_enum", create_type=False
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["helper_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", "helper_id", name="uq_booking_offers_booking_helper"),
    )
    op.create_index(
        op.f("ix_booking_offers_booking_id"), "booking_offers", ["booking_id"], unique=False
    )
    op.create_index(
        op.f("ix_booking_offers_helper_id"), "booking_offers", ["helper_id"], unique=False
    )
    op.create_index(op.f("ix_booking_offers_status"), "booking_offers", ["status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index(op.f("ix_booking_offers_status"), table_name="booking_offers")
    op.drop_index(op.f("ix_booking_offers_helper_id"), table_name="booking_offers")
    op.drop_index(op.f("ix_booking_offers_booking_id"), table_name="booking_offers")
    op.drop_table("booking_offers")

    op.drop_index("uq_bookings_active_helper", table_name="bookings")
    op.drop_constraint("ck_bookings_duration_hours_range", "bookings", type_="check")
    op.drop_constraint("fk_bookings_cancelled_by_users", "bookings", type_="foreignkey")
    op.drop_column("bookings", "cancelled_by")
    op.drop_column("bookings", "cancellation_reason")
    op.drop_column("bookings", "max_price")
    op.drop_column("bookings", "min_price")
    op.drop_column("bookings", "matching_mode")
    op.drop_column("bookings", "duration_hours")
    op.drop_column("bookings", "urgency")

    postgresql.ENUM(name="booking_offer_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="booking_matching_mode_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="booking_urgency_enum").drop(bind, checkfirst=True)
