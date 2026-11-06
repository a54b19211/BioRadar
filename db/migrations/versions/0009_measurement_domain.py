"""measurement domain: the comparable domain of a checked point - basis and id. An existing point whose
candidate was placed in a topic gets that topic (the record's placement, as adopt now reads it; not
measurements.topic_id, which a series-opened topic also set); every other existing point gets basis "unknown":
stored and effective, nothing computed from it."""

import sqlalchemy as sa
from alembic import op

revision = "0009_measurement_domain"
down_revision = "0008_record_reads"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "measurements", sa.Column("domain_basis", sa.String(20), nullable=False, server_default="unknown")
    )
    op.add_column("measurements", sa.Column("domain_id", sa.String(64)))
    op.execute(
        "UPDATE measurements SET domain_basis = 'topic', domain_id = c.topic_id FROM measurement_candidates c"
        " WHERE measurements.candidate_id = c.id AND c.topic_id IS NOT NULL"
    )
    op.create_index(
        "ix_measurements_domain", "measurements", ["workspace_id", "is_demo", "domain_basis", "domain_id"]
    )


def downgrade():
    op.drop_index("ix_measurements_domain", table_name="measurements")
    op.drop_column("measurements", "domain_id")
    op.drop_column("measurements", "domain_basis")
