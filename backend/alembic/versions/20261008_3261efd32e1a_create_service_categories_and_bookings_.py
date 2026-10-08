"""create service_categories and bookings tables

Revision ID: 3261efd32e1a
Revises: b9de14bd6155
Create Date: 2026-10-08 15:15:11.414838+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3261efd32e1a"
down_revision: Union[str, Sequence[str], None] = "b9de14bd6155"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BOOKING_STATUSES = (
    "requested",
    "matched",
    "accepted",
    "en_route",
    "in_progress",
    "completed",
    "cancelled",
    "disputed",
)


def upgrade() -> None:
    # The enum type must be created explicitly. Letting create_table emit it
    # means drop_table will not clean it up on downgrade, leaving an orphan that
    # makes every subsequent upgrade fail with DuplicateObjectError.
    postgresql.ENUM(*BOOKING_STATUSES, name="booking_status_enum").create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "service_categories",
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_price_ngn", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_service_categories_name"), "service_categories", ["name"], unique=True)
    op.create_index(op.f("ix_service_categories_slug"), "service_categories", ["slug"], unique=True)
    op.create_table(
        "bookings",
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("helper_id", sa.UUID(), nullable=True),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*BOOKING_STATUSES, name="booking_status_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recommended_price", sa.Float(), nullable=True),
        sa.Column("agreed_price", sa.Float(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["service_categories.id"],
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["helper_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bookings_customer_id"), "bookings", ["customer_id"], unique=False)
    op.create_index(op.f("ix_bookings_helper_id"), "bookings", ["helper_id"], unique=False)
    op.create_index(op.f("ix_bookings_status"), "bookings", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bookings_status"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_helper_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_customer_id"), table_name="bookings")
    op.drop_table("bookings")
    op.drop_index(op.f("ix_service_categories_slug"), table_name="service_categories")
    op.drop_index(op.f("ix_service_categories_name"), table_name="service_categories")
    op.drop_table("service_categories")
    postgresql.ENUM(name="booking_status_enum").drop(op.get_bind(), checkfirst=True)
