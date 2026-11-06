"""measurement candidates relocated from an older artifact (T-26): a known fact of an older artifact of the
version located again in an upgraded artifact by its quote names the candidate it came from. Existing rows
have none."""

import sqlalchemy as sa
from alembic import op

revision = "0016_candidate_relocated_from"
down_revision = "0015_artifact_upkeep"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("measurement_candidates", sa.Column("relocated_from", sa.Integer))
    op.create_index("ix_measurement_candidates_relocated_from", "measurement_candidates", ["relocated_from"])


def downgrade():
    op.drop_index("ix_measurement_candidates_relocated_from", "measurement_candidates")
    op.drop_column("measurement_candidates", "relocated_from")
