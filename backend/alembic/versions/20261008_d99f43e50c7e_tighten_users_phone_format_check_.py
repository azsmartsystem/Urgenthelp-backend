"""tighten users phone format check constraint

Revision ID: d99f43e50c7e
Revises: 26db6f25013c
Create Date: 2026-10-08 17:37:26.625984+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d99f43e50c7e"
down_revision: Union[str, Sequence[str], None] = "26db6f25013c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Revision 26db6f25013c shipped '^234[0-9]{10}$', which is looser than
    # normalize_nigerian_phone in app/core/validators.py (^234[789]\d{9}$).
    # That revision is already applied and pushed, and Alembic never re-runs an
    # applied revision, so the widening has to be corrected here rather than by
    # editing history.
    #
    # Alembic does not diff CHECK constraints, so `alembic check` cannot detect
    # this class of drift; tests/test_validators.py pins the model and validator
    # together, and this revision keeps the database in step with them.
    op.drop_constraint("ck_users_phone_format", "users", type_="check")
    op.create_check_constraint(
        "ck_users_phone_format",
        "users",
        "phone ~ '^234[789][0-9]{9}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_phone_format", "users", type_="check")
    op.create_check_constraint(
        "ck_users_phone_format",
        "users",
        "phone ~ '^234[0-9]{10}$'",
    )
