"""Ordinary bounded machine analysis records, separate from scientific analysis_runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_investigation_analyses"
down_revision = "0007_topics"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "investigation_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("investigation_id", sa.String(36), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("parent_analysis_id", sa.String(36), sa.ForeignKey("investigation_analyses.id")),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("based_on_revision", sa.Integer(), nullable=False),
        sa.Column("fixed_input", postgresql.JSONB(), nullable=False),
        sa.Column("progress", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("dispatch_pending", sa.Boolean(), nullable=False),
        sa.Column("rq_job_id", sa.String(100), nullable=False),
        sa.Column("execution_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("note_entry_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "workspace_id", "investigation_id", "input_hash", name="uq_investigation_analysis"
        ),
        sa.CheckConstraint(
            "state IN ('queued','running','completed','needs_review','blocked','failed')",
            name="ck_investigation_analysis_state",
        ),
    )
    op.create_index("ix_investigation_analyses_workspace", "investigation_analyses", ["workspace_id"])
    op.create_index(
        "ix_investigation_analyses_history",
        "investigation_analyses",
        ["investigation_id", "created_at", "id"],
    )
    op.create_index("ix_investigation_analyses_dispatch", "investigation_analyses", ["state", "created_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM investigation_analyses")):
        raise RuntimeError("Export immutable machine analyses before explicitly removing the table")
    op.drop_table("investigation_analyses")
