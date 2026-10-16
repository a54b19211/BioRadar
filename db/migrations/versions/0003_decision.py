"""Two fixed analysis kinds share the existing execution state and immutable history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_decision"
down_revision = "0002_engine"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "analysis_runs",
        sa.Column("run_kind", sa.String(20), nullable=False, server_default="mining"),
    )
    op.add_column("analysis_runs", sa.Column("decision_input", postgresql.JSONB(), nullable=True))
    op.alter_column("analysis_runs", "snapshot_id", existing_type=sa.String(36), nullable=True)
    op.create_check_constraint(
        "ck_analysis_run_input_kind",
        "analysis_runs",
        "(run_kind = 'mining' AND snapshot_id IS NOT NULL AND decision_input IS NULL) OR "
        "(run_kind = 'decision' AND snapshot_id IS NULL AND decision_input IS NOT NULL)",
    )
    op.create_index("ix_analysis_runs_workspace_kind", "analysis_runs", ["workspace_id", "run_kind"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM analysis_runs WHERE run_kind = 'decision'")):
        raise RuntimeError("Cannot discard persisted decision history when downgrading")
    op.drop_index("ix_analysis_runs_workspace_kind", table_name="analysis_runs")
    op.drop_constraint("ck_analysis_run_input_kind", "analysis_runs", type_="check")
    op.alter_column("analysis_runs", "snapshot_id", existing_type=sa.String(36), nullable=False)
    op.drop_column("analysis_runs", "decision_input")
    op.drop_column("analysis_runs", "run_kind")
