"""Persist the no-keyboard campaign progress message identifier.

Revision ID: 0004_campaign_progress
Revises: 0003_campaign_setup
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_campaign_progress"
down_revision: str | None = "0003_campaign_setup"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("progress_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "progress_message_id")
