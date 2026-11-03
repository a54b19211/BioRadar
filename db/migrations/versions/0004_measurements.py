"""Measurements: quoted values per object, metric and period, accumulated from every triaged record (T-24);
observations keep Fabric's same-origin group so daily activity counts reposts once (T-15)."""

import sqlalchemy as sa
from alembic import op

revision = "0004_measurements"
down_revision = "0003_cursor_last_new"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "measurements",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("object", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(200), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False, server_default=""),
        sa.Column("kind", sa.String(20), nullable=False, server_default="other"),
        sa.Column("scope", sa.String(200), nullable=False, server_default=""),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("numerator", sa.Float),
        sa.Column("denominator", sa.Float),
        sa.Column("period", sa.String(40), nullable=False, server_default=""),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="other"),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("quote", sa.Text, nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id")),
        sa.Column("superseded_by", sa.Integer),
        sa.UniqueConstraint("version_id", "object", "metric", "unit", "kind", "scope", "period"),
    )
    op.create_index("ix_measurements_object", "measurements", ["object"])
    op.create_index("ix_measurements_topic_id", "measurements", ["topic_id"])
    op.create_index(
        "ix_measurements_series",
        "measurements",
        ["workspace_id", "is_demo", "object", "metric", "unit", "kind", "scope"],
    )


    op.add_column("observations", sa.Column("origin_group", sa.String(200)))


def downgrade():
    op.drop_column("observations", "origin_group")
    op.drop_table("measurements")
