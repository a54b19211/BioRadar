"""Immutable historical predictions and independent append-only evaluation records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_history"
down_revision = "0004_investigations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "historical_studies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("prediction_hash", sa.String(64)),
        sa.Column("investigation_id", sa.String(36), sa.ForeignKey("investigations.id")),
        sa.CheckConstraint(
            "state IN ('collecting','predicted','partial','unsupported','failed')",
            name="ck_history_study_state",
        ),
    )
    op.create_index("ix_historical_studies_workspace_id", "historical_studies", ["workspace_id"])
    op.create_table(
        "historical_evaluations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("study_id", sa.String(36), sa.ForeignKey("historical_studies.id"), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prediction_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("result_hash", sa.String(64)),
        sa.CheckConstraint(
            "state IN ('collecting','completed','partial','failed')", name="ck_history_evaluation_state"
        ),
    )
    op.create_index("ix_historical_evaluations_study_id", "historical_evaluations", ["study_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM historical_studies")):
        raise RuntimeError("Saved predictions/evaluations cannot be discarded by downgrade")
    op.drop_index("ix_historical_evaluations_study_id", table_name="historical_evaluations")
    op.drop_table("historical_evaluations")
    op.drop_index("ix_historical_studies_workspace_id", table_name="historical_studies")
    op.drop_table("historical_studies")
