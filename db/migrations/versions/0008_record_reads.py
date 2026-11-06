"""record reads: the reading plan of each triaged fixed version, its windows and every attempt.

Versions triaged before this migration have no plan: only their first window was read. They are not given one
here (that would claim a plan was made at triage time); the coverage report counts them as "no plan"."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_record_reads"
down_revision = "0007_measurement_checks"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "record_reads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False, index=True),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("part", JSONB, nullable=False),
        sa.Column("processor_version", sa.String(120)),
        sa.Column("text_sha256", sa.String(64)),
        sa.Column("total_chars", sa.Integer, nullable=False),
        sa.Column("window_chars", sa.Integer, nullable=False),
        sa.Column("overlap_chars", sa.Integer, nullable=False),
        sa.Column("windows_total", sa.Integer, nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(120)),
        sa.Column("read_until", sa.Integer, nullable=False),
        sa.Column("topic_id", sa.String(36)),
        sa.Column("direction", sa.String(30), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relations", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "is_demo", "version_id"),
    )
    op.create_table(
        "record_read_windows",
        sa.Column("read_id", sa.Integer, sa.ForeignKey("record_reads.id"), primary_key=True),
        sa.Column("window_index", sa.Integer, primary_key=True),
        sa.Column("start_char", sa.Integer, nullable=False),
        sa.Column("end_char", sa.Integer, nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "record_read_attempts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("read_id", sa.Integer, sa.ForeignKey("record_reads.id"), nullable=False, index=True),
        sa.Column("window_index", sa.Integer, nullable=False),
        sa.Column("outcome", sa.String(60), nullable=False),
        sa.Column("material_read_id", sa.Integer),
        sa.Column("llm_call_id", sa.Integer),
        sa.Column("measures_found", sa.Integer),
        sa.Column("measures_enqueued", sa.Integer),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "CREATE RULE record_read_attempts_no_update AS ON UPDATE TO record_read_attempts DO INSTEAD NOTHING"
    )
    op.execute(
        "CREATE RULE record_read_attempts_no_delete AS ON DELETE TO record_read_attempts DO INSTEAD NOTHING"
    )


def downgrade():
    op.drop_table("record_read_attempts")
    op.drop_table("record_read_windows")
    op.drop_table("record_reads")
