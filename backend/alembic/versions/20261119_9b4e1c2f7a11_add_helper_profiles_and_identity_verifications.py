"""add helper_profiles, helper_categories, and identity_verifications tables

Revision ID: 9b4e1c2f7a11
Revises: 8a3f2d1c9e04
Create Date: 2026-11-19 21:05:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9b4e1c2f7a11"
down_revision: Union[str, Sequence[str], None] = "8a3f2d1c9e04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "helper_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column(
            "service_radius_km",
            sa.Float(),
            server_default=sa.text("10.0"),
            nullable=False,
        ),
        sa.Column(
            "is_available",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("availability_schedule", postgresql.JSONB(), nullable=True),
        sa.Column(
            "certifications",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "avg_rating",
            sa.Float(),
            server_default=sa.text("0.0"),
            nullable=False,
        ),
        sa.Column(
            "completed_jobs_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "service_radius_km > 0 AND service_radius_km <= 50",
            name="ck_helper_profiles_service_radius_range",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_helper_profiles_user_id", "helper_profiles", ["user_id"], unique=True)
    op.create_index(
        "ix_helper_profiles_is_available", "helper_profiles", ["is_available"], unique=False
    )

    op.create_table(
        "helper_categories",
        sa.Column("helper_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["helper_profile_id"], ["helper_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["service_categories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("helper_profile_id", "category_id"),
    )
    op.create_index(
        "ix_helper_categories_category_id", "helper_categories", ["category_id"], unique=False
    )

    op.create_table(
        "identity_verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_type",
            sa.Enum(
                "nin",
                "drivers_license",
                "voters_card",
                "international_passport",
                name="id_document_type_enum",
            ),
            nullable=False,
        ),
        sa.Column("document_url", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="verification_status_enum"),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identity_verifications_user_id",
        "identity_verifications",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_verifications_status",
        "identity_verifications",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_identity_verifications_status", table_name="identity_verifications")
    op.drop_index("ix_identity_verifications_user_id", table_name="identity_verifications")
    op.drop_table("identity_verifications")
    sa.Enum(name="verification_status_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="id_document_type_enum").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_helper_categories_category_id", table_name="helper_categories")
    op.drop_table("helper_categories")

    op.drop_index("ix_helper_profiles_is_available", table_name="helper_profiles")
    op.drop_index("ix_helper_profiles_user_id", table_name="helper_profiles")
    op.drop_table("helper_profiles")
