"""Fabric's measurement objects in the radar (docs/设计.md 决策 0010).

measurement_inbox: every Fabric measurement object this workspace has heard of (the change feed or a pull by
version) and what the radar did with it. measurement_withheld: per source, the feed events Fabric withheld
while the source could not be read, until its versions are pulled again. series_points: which topic put
which Fabric object into which of its series - the investigation's selection, its gate and series key.
The radar's own measurement tables are not touched here."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_fabric_measurements"
down_revision = "0021_mech_attempt_ledger"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "measurement_inbox",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("measurement_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False, index=True),
        sa.Column("source_id", sa.String(80), nullable=False),
        sa.Column("last_seq", sa.Integer, nullable=False),
        sa.Column("last_kind", sa.String(40), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("due", sa.Boolean, nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("workspace_id", "is_demo", "measurement_id", name="measurement_inbox_object"),
    )
    op.create_index(
        "ix_measurement_inbox_due", "measurement_inbox", ["workspace_id", "is_demo", "due", "updated_at"]
    )
    op.create_table(
        "measurement_withheld",
        sa.Column("workspace_id", sa.String(100), primary_key=True),
        sa.Column("is_demo", sa.Boolean, primary_key=True),
        sa.Column("source_id", sa.String(80), primary_key=True),
        sa.Column("events", sa.Integer, nullable=False),
        sa.Column("last_seq", sa.Integer, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "series_points",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id"), nullable=False, index=True),
        sa.Column("measurement_id", sa.String(64), nullable=False, index=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("object", sa.String(300), nullable=False),
        sa.Column("metric", sa.String(200), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("measure_kind", sa.String(20), nullable=False),
        sa.Column("scope", sa.String(200), nullable=False),
        sa.Column("scope_mode", sa.String(30), nullable=False),
        sa.Column("period", sa.String(40), nullable=False),
        sa.Column("period_type", sa.String(10), nullable=False),
        sa.Column("period_precision", sa.String(10), nullable=False),
        sa.Column("period_start", TS, nullable=False),
        sa.Column("period_end", TS, nullable=False),
        sa.Column("value_text", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("quote", sa.Text, nullable=False),
        sa.Column("object_key", sa.String(900), nullable=False),
        sa.Column("joined_by_rule", sa.String(40), nullable=False),
        sa.Column("family", sa.String(30), nullable=False),
        sa.Column("dims", postgresql.JSONB, nullable=False),
        sa.Column("dims_key", sa.String(80), nullable=False),
        sa.Column("gate_version", sa.String(40), nullable=False),
        sa.Column("gate_reasons", postgresql.JSONB, nullable=False),
        sa.Column("series_key", sa.String(80), nullable=False),
        sa.Column("check_status", sa.String(20), nullable=False),
        sa.Column("check_ref", sa.String(80), nullable=False),
        sa.Column("checker_version", sa.String(80), nullable=False),
        sa.Column("available_at", TS, nullable=False),
        sa.Column("joined_at", TS, nullable=False),
        sa.Column("left_at", TS),
        sa.Column("left_reason", sa.String(60)),
        sa.Column("left_by", sa.String(64)),
        sa.UniqueConstraint(
            "workspace_id", "is_demo", "topic_id", "measurement_id", name="series_points_topic_object"
        ),
    )
    op.create_index(
        "ix_series_points_series", "series_points", ["workspace_id", "is_demo", "topic_id", "series_key"]
    )


def downgrade():
    """Refused (the run rolls back) while a topic has selected any object: the selection is not deleted by an
    ordinary downgrade."""
    held = op.get_bind().execute(sa.text("SELECT count(*) FROM series_points")).scalar() or 0
    if held:
        raise RuntimeError(f"downgrade refused: {held} series_points - back up and settle first")
    op.drop_table("series_points")
    op.drop_table("measurement_withheld")
    op.drop_table("measurement_inbox")
