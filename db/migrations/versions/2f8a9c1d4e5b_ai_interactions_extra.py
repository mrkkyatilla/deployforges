"""ai_interactions.extra JSONB for Gemini IO excerpts / parse metadata.

Revision ID: 2f8a9c1d4e5b
Revises: 1d1a18fa5be2
Create Date: 2026-05-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "2f8a9c1d4e5b"
down_revision: Union[str, None] = "1d1a18fa5be2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_interactions",
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_interactions", "extra")
