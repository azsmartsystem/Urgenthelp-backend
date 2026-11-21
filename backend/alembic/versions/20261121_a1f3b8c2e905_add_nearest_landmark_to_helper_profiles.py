"""add nearest_landmark to helper_profiles

Revision ID: a1f3b8c2e905
Revises: 9b4e1c2f7a11
Create Date: 2026-11-21 13:27:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f3b8c2e905"
down_revision: Union[str, Sequence[str], None] = "9b4e1c2f7a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "helper_profiles",
        sa.Column("nearest_landmark", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("helper_profiles", "nearest_landmark")
