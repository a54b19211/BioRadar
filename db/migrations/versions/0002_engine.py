"""Persisted development research engine; no formal topic/delivery business."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_engine"
down_revision = "0001_boot"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "engine_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("source_instance", sa.String(200), nullable=False),
        sa.Column("grant_ref", sa.String(200), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "snapshot_hash"),
    )
    op.create_index("ix_engine_snapshots_workspace_id", "engine_snapshots", ["workspace_id"])
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("engine_snapshots.id"), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("method_versions", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("accept_token", sa.String(64), nullable=False),
        sa.Column("dispatch_pending", sa.Boolean(), nullable=False),
        sa.Column("rq_job_id", sa.String(100), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(500)),
        sa.Column(
            "attempt_diagnostics", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "input_hash"),
        sa.CheckConstraint("state in ('queued','running','completed','partial','failed')"),
        sa.CheckConstraint("attempt >= 1 and revision >= 1"),
    )
    op.create_index("ix_analysis_runs_workspace_id", "analysis_runs", ["workspace_id"])
    op.create_table(
        "engine_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("candidate_key", sa.String(200), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_engine_feedback_workspace_id", "engine_feedback", ["workspace_id"])
    op.create_index("ix_engine_feedback_run_id", "engine_feedback", ["run_id"])
    op.create_table(
        "engine_idempotency",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("operation", sa.String(200), nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column("response_code", sa.Integer()),
        sa.Column("response_body", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "actor_id", "operation", "key"),
    )


def downgrade():
    op.drop_table("engine_idempotency")
    op.drop_index("ix_engine_feedback_run_id", table_name="engine_feedback")
    op.drop_index("ix_engine_feedback_workspace_id", table_name="engine_feedback")
    op.drop_table("engine_feedback")
    op.drop_index("ix_analysis_runs_workspace_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_index("ix_engine_snapshots_workspace_id", table_name="engine_snapshots")
    op.drop_table("engine_snapshots")
