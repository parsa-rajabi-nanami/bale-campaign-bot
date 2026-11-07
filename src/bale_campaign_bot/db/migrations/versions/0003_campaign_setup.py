"""Persist campaign audience and optional follow-up CTA configuration.

Revision ID: 0003_campaign_setup
Revises: 0002_single_owner
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_campaign_setup"
down_revision: str | None = "0002_single_owner"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("audience_spec", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "campaigns",
        sa.Column("cta_buttons", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "cta_buttons")
    op.drop_column("campaigns", "audience_spec")
