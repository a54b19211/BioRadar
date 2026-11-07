"""topics.next_prop_seq: the number the next proposition of a topic's analyses takes
(research/propositions.py). It is read and raised under the topic row's lock in the transaction that writes
an analysis result. Existing topics start at 1.

Downgrading drops the counters, so it is refused once any topic has handed a number out: the ids already in
`analyses.result` would be handed out again after the next upgrade."""

import sqlalchemy as sa
from alembic import op

revision = "0024_topic_prop_seq"
down_revision = "0023_freeze_measurement_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("topics", sa.Column("next_prop_seq", sa.Integer, nullable=False, server_default="1"))


def downgrade():
    count = sa.text("SELECT count(*) FROM topics WHERE next_prop_seq > 1")
    numbered = op.get_bind().execute(count).scalar() or 0
    if numbered:
        raise RuntimeError(
            f"downgrade refused, numbers kept: {numbered} topics have numbered propositions "
            "(topics.next_prop_seq) - back up and settle first"
        )
    op.drop_column("topics", "next_prop_seq")
