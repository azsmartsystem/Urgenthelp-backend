"""seed canonical PRD service categories

Revision ID: 7c4a9e1b2d08
Revises: 30ec41bff66d
Create Date: 2026-11-18 21:40:00.000000+00:00

"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7c4a9e1b2d08"
down_revision: Union[str, Sequence[str], None] = "30ec41bff66d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SEED_SPECS: tuple[tuple[str, str, str, float], ...] = (
    (
        "cleaning",
        "Cleaning",
        "Standard home cleaning, sweeping, mopping, and surface tidying.",
        5000.0,
    ),
    (
        "laundry",
        "Laundry",
        "Washing, drying, ironing, and folding of clothes and linens.",
        3000.0,
    ),
    (
        "cooking",
        "Cooking",
        "Home meal preparation, batch cooking, and kitchen cleanup.",
        4000.0,
    ),
    (
        "babysitting",
        "Babysitting",
        "Trusted childcare, supervision, and basic child assistance at home.",
        4000.0,
    ),
    (
        "elder_care",
        "Elder Care",
        "Companionship, mobility support, and non-medical assistance for seniors.",
        6000.0,
    ),
    (
        "office_cleaning",
        "Office Cleaning",
        "Commercial workspace sanitization, desk cleaning, and waste disposal.",
        8000.0,
    ),
    (
        "office_assistance",
        "Office Assistance",
        "On-demand clerical support, filing, errands, and front-desk help.",
        5000.0,
    ),
    (
        "plumbing",
        "Plumbing",
        "Pipe leak repairs, drain unblocking, tap replacement, and toilet fixes.",
        8000.0,
    ),
    (
        "electrical_work",
        "Electrical Work",
        "Wiring diagnostics, socket and switch repairs, and lighting installation.",
        8000.0,
    ),
    (
        "carpentry",
        "Carpentry",
        "Furniture assembly, door and hinge repairs, shelving, and woodwork.",
        7000.0,
    ),
    (
        "painting",
        "Painting",
        "Interior and exterior wall painting, touch-ups, and surface prep.",
        6000.0,
    ),
    (
        "generator_repair",
        "Generator Repair",
        "Generator servicing, fault troubleshooting, and parts replacement.",
        7000.0,
    ),
    (
        "ac_repair",
        "Air-Conditioner Repair",
        "AC unit servicing, gas refilling, fault repair, and installation.",
        9000.0,
    ),
    (
        "appliance_repair",
        "Appliance Repair",
        "Diagnostics and repair for fridges, washers, microwaves, and TVs.",
        7000.0,
    ),
    (
        "gardening",
        "Gardening",
        "Lawn mowing, hedge trimming, weeding, and compound upkeep.",
        4000.0,
    ),
    (
        "driving",
        "Driving",
        "Verified personal driver hire for errands, commutes, and trips.",
        3500.0,
    ),
    (
        "dispatch_services",
        "Dispatch Services",
        "Same-day parcel pickup, document delivery, and local errands.",
        3000.0,
    ),
    (
        "home_tutoring",
        "Home Tutoring",
        "One-on-one academic coaching and exam prep at your location.",
        5000.0,
    ),
    (
        "pet_care",
        "Pet Care",
        "Dog walking, pet sitting, feeding, and basic grooming support.",
        4000.0,
    ),
)


def _category_id(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"urgenthelp.service_category.{slug}")


SEED_CATEGORIES: list[dict[str, object]] = [
    {
        "id": _category_id(slug),
        "name": name,
        "slug": slug,
        "description": description,
        "base_price_ngn": base_price_ngn,
        "is_active": True,
    }
    for slug, name, description, base_price_ngn in _SEED_SPECS
]

SEEDED_SLUGS: tuple[str, ...] = tuple(slug for slug, _, _, _ in _SEED_SPECS)
SEEDED_IDS: tuple[uuid.UUID, ...] = tuple(_category_id(slug) for slug, _, _, _ in _SEED_SPECS)

_service_categories_table = sa.table(
    "service_categories",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.String(length=50)),
    sa.column("slug", sa.String(length=50)),
    sa.column("description", sa.Text()),
    sa.column("base_price_ngn", sa.Float()),
    sa.column("is_active", sa.Boolean()),
)

_bookings_table = sa.table(
    "bookings",
    sa.column("category_id", postgresql.UUID(as_uuid=True)),
)


def build_upgrade_stmt() -> postgresql.Insert:
    """Build idempotent INSERT ... ON CONFLICT DO NOTHING statement."""
    return (
        postgresql.insert(_service_categories_table)
        .values(SEED_CATEGORIES)
        .on_conflict_do_nothing()
    )


def build_downgrade_stmt() -> sa.Delete:
    """Build DELETE removing only unreferenced rows seeded by this migration.

    Filtering by both ``SEEDED_IDS`` and ``SEEDED_SLUGS`` ensures pre-existing
    rows skipped during ``upgrade()`` are never removed, and ``~has_booking_ref``
    keeps any seeded category already referenced by ``bookings.category_id``
    so downgrade never fails with a ForeignKeyViolationError.
    """
    has_booking_ref = sa.exists(
        sa.select(sa.literal(1)).where(
            _bookings_table.c.category_id == _service_categories_table.c.id
        )
    )
    return (
        sa.delete(_service_categories_table)
        .where(_service_categories_table.c.id.in_(SEEDED_IDS))
        .where(_service_categories_table.c.slug.in_(SEEDED_SLUGS))
        .where(~has_booking_ref)
    )


def upgrade() -> None:
    op.execute(build_upgrade_stmt())


def downgrade() -> None:
    op.execute(build_downgrade_stmt())
