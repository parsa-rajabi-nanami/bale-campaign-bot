"""Track the completion summary delivery attempt.

Revision ID: 0005_campaign_summary
Revises: 0004_campaign_progress
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_campaign_summary"
down_revision: str | None = "0004_campaign_progress"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("summary_sent_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("campaigns", "summary_sent_at")
