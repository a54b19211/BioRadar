"""material_reads: what each model or service actually received, with its binding (v3 6.1)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_material_reads"
down_revision = "0005_users_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "material_reads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), index=True),
        sa.Column("llm_call_id", sa.Integer, sa.ForeignKey("llm_calls.id"), index=True),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("service", sa.String(30), nullable=False),
        sa.Column("ref", sa.String(20), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("chars", sa.Integer, nullable=False),
        sa.Column("binding", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("material_reads")
