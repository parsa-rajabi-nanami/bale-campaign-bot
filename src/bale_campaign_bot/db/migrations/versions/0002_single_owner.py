"""Guarantee that at most one database admin has the owner role.

Revision ID: 0002_single_owner
Revises: 0001_initial
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_single_owner"
down_revision: str | None = "0001_initial"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_single_admin_owner",
        "admins",
        ["role"],
        unique=True,
        sqlite_where=sa.text("role = 'owner'"),
        postgresql_where=sa.text("role = 'owner'"),
    )


def downgrade() -> None:
    op.drop_index("uq_single_admin_owner", table_name="admins")
