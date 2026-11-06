"""measurement dims (the complete comparable domain): the metric family, the required dimensions with their
state and proof, their group key, and the gaps that keep a point out of automatic comparison. Existing points
get no family and the gap family_not_determined: stored and effective, never compared automatically again
until they are checked with their dimensions."""

import sqlalchemy as sa
from alembic import op

revision = "0012_measurement_dims"
down_revision = "0011_candidate_material_read"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("measurements", sa.Column("family", sa.String(30), nullable=False, server_default=""))
    op.add_column(
        "measurements", sa.Column("dims", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
    )
    op.add_column("measurements", sa.Column("dims_key", sa.String(400), nullable=False, server_default=""))
    op.add_column(
        "measurements",
        sa.Column(
            "dims_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[\"family_not_determined\"]'")
        ),
    )


def downgrade():
    op.drop_column("measurements", "dims_reasons")
    op.drop_column("measurements", "dims_key")
    op.drop_column("measurements", "dims")
    op.drop_column("measurements", "family")
