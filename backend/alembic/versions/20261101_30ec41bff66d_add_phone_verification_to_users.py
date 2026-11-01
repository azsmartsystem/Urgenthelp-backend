"""add phone verification to users

Revision ID: 30ec41bff66d
Revises: d99f43e50c7e
Create Date: 2026-11-01 16:46:25.619449+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "30ec41bff66d"
down_revision: Union[str, Sequence[str], None] = "d99f43e50c7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_phone_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "phone_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "is_phone_verified")
