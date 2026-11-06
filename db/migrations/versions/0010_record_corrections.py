"""record corrections: a correction or retraction Fabric states for a record, with the
time it became visible - its own lifecycle, apart from whether a replacement value passes checking. Starts
empty: the corrections of records already observed are recorded again when those records are read again."""

import sqlalchemy as sa
from alembic import op

revision = "0010_record_corrections"
down_revision = "0009_measurement_domain"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "record_corrections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("target_source", sa.String(80), nullable=False),
        sa.Column("target_record_id", sa.String(400), nullable=False),
        sa.Column("target_version_id", sa.String(128), nullable=False),
        sa.Column("relation", sa.String(20), nullable=False),
        sa.Column("by_source", sa.String(80), nullable=False),
        sa.Column("by_record_id", sa.String(400), nullable=False),
        sa.Column("by_version_id", sa.String(128), nullable=False),
        sa.Column("visible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "is_demo",
            "target_source",
            "target_record_id",
            "target_version_id",
            "by_version_id",
        ),
    )
    op.create_index(
        "ix_record_corrections_target",
        "record_corrections",
        ["workspace_id", "is_demo", "target_source", "target_record_id"],
    )


def downgrade():
    op.drop_index("ix_record_corrections_target", table_name="record_corrections")
    op.drop_table("record_corrections")
