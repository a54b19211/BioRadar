"""Monitor cursors remember when a source last delivered new records (source status page)."""

import sqlalchemy as sa
from alembic import op

revision = "0003_cursor_last_new"
down_revision = "0002_topic_pending"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("monitor_cursors", sa.Column("last_new_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("monitor_cursors", "last_new_at")
