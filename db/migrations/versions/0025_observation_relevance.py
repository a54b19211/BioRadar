"""observations.relevance and observations.direction: what the triage model judged of a record - whether it is
medical or public-health intelligence at all (related | unrelated | uncertain) and in which direction
(research/triage.py). Null: no model judged it (a failed call, a record the program settled, a row from before
this column). The candidate groups leave out what was judged unrelated (research/groups.py).

Downgrading drops the judgments; they are asked again when a record is next triaged."""

import sqlalchemy as sa
from alembic import op

revision = "0025_observation_relevance"
down_revision = "0024_topic_prop_seq"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("observations", sa.Column("relevance", sa.String(12)))
    op.add_column("observations", sa.Column("direction", sa.String(20)))


def downgrade():
    op.drop_column("observations", "direction")
    op.drop_column("observations", "relevance")
