"""add phone format check constraint to users table

Revision ID: 26db6f25013c
Revises: 3261efd32e1a
Create Date: 2026-10-08 17:04:22.035462+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "26db6f25013c"
down_revision: Union[str, Sequence[str], None] = "3261efd32e1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_users_phone_format",
        "users",
        "phone ~ '^234[0-9]{10}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_phone_format", "users", type_="check")
