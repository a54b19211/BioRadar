"""Topics remember an automatic analysis they are owed until it is created; model calls record how
many HTTP requests they took, so max_llm_calls counts format retries."""

import sqlalchemy as sa
from alembic import op

revision = "0002_topic_pending"
down_revision = "0001_rebuild"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("topics", sa.Column("pending_reason", sa.String(200)))
    op.add_column("topics", sa.Column("pending_since", sa.DateTime(timezone=True)))
    op.add_column("llm_calls", sa.Column("requests", sa.Integer, nullable=False, server_default="1"))


def downgrade():
    op.drop_column("llm_calls", "requests")
    op.drop_column("topics", "pending_since")
    op.drop_column("topics", "pending_reason")
