"""Persist monitor progress, immutable batch inputs and deduplicated investigation leads."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_monitor"
down_revision = "0005_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monitor_state",
        sa.Column("workspace_id", sa.String(100), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("source_progress", postgresql.JSONB(), nullable=False),
        sa.Column("next_source", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
    )
    op.create_table(
        "monitor_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("batch_key", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("engine_snapshots.id")),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("analysis_runs.id")),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "batch_key", name="uq_monitor_batch_input"),
        sa.CheckConstraint(
            "state IN ('accepted','queued','investigating','completed','blocked')",
            name="ck_monitor_batch_state",
        ),
    )
    op.create_index("ix_monitor_batches_workspace_id", "monitor_batches", ["workspace_id"])
    op.create_table(
        "monitor_leads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("lead_key", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("monitor_batches.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("investigation_id", sa.String(36), sa.ForeignKey("investigations.id")),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("support_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "lead_key", name="uq_monitor_lead"),
        sa.CheckConstraint(
            "state IN ('pending','investigating','investigated','deferred','blocked')",
            name="ck_monitor_lead_state",
        ),
    )
    op.create_index("ix_monitor_leads_workspace_id", "monitor_leads", ["workspace_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM monitor_batches")):
        raise RuntimeError(
            "Saved monitor evidence and investigation receipts require explicit export before downgrade"
        )
    op.drop_index("ix_monitor_leads_workspace_id", table_name="monitor_leads")
    op.drop_table("monitor_leads")
    op.drop_index("ix_monitor_batches_workspace_id", table_name="monitor_batches")
    op.drop_table("monitor_batches")
    op.drop_table("monitor_state")
