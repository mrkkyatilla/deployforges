"""projects.final_manifest, manifest_version; builds.service_name.

Revision ID: 3a7b2c9d1e4f
Revises: 2f8a9c1d4e5b
Create Date: 2026-05-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "3a7b2c9d1e4f"
down_revision: Union[str, None] = "2f8a9c1d4e5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("final_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("manifest_version", sa.String(length=16), server_default="1", nullable=True),
    )
    op.add_column(
        "builds",
        sa.Column("service_name", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("builds", "service_name")
    op.drop_column("projects", "manifest_version")
    op.drop_column("projects", "final_manifest")
