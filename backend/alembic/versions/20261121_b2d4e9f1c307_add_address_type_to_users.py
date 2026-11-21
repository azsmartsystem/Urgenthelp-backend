"""add address_type to users

Revision ID: b2d4e9f1c307
Revises: a1f3b8c2e905
Create Date: 2026-11-21 14:11:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d4e9f1c307"
down_revision: Union[str, Sequence[str], None] = "a1f3b8c2e905"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_address_type_enum = sa.Enum("home", "work", "other", name="address_type_enum")


def upgrade() -> None:
    _address_type_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "users",
        sa.Column("address_type", _address_type_enum, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "address_type")
    _address_type_enum.drop(op.get_bind(), checkfirst=True)
