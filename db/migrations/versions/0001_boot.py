"""Framework probe only. Business migrations follow A01/A02/B01 ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0001_boot"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "infrastructure_probes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("infrastructure_probes")
