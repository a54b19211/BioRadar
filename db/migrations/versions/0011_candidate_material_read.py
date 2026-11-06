"""candidate material read: the material_read of the window a candidate was read from.
Existing candidates keep NULL: the send they came from was not recorded with them, so no source of theirs can
be shown sent - they keep the measure.v5 locating rules and cite no other source."""

import sqlalchemy as sa
from alembic import op

revision = "0011_candidate_material_read"
down_revision = "0010_record_corrections"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "measurement_candidates",
        sa.Column("material_read_id", sa.Integer(), sa.ForeignKey("material_reads.id"), nullable=True),
    )
    op.create_index(
        "ix_measurement_candidates_material_read_id", "measurement_candidates", ["material_read_id"]
    )


def downgrade():
    op.drop_index("ix_measurement_candidates_material_read_id", table_name="measurement_candidates")
    op.drop_column("measurement_candidates", "material_read_id")
