"""Persist detailed Breadth flowgraph snapshots per repository.

Revision ID: 0006_breadth_graph_snapshot
Revises: 0005_architecture_improvements
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0006_breadth_graph_snapshot"
down_revision = "0005_architecture_improvements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "breadth_graph_snapshots",
        sa.Column(
            "repo_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("flowgraph", JSONB, nullable=False),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("breadth_graph_snapshots")
