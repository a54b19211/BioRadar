"""llm_outputs (measure.v10, Codex): every answer of a model call as given - content and sha256 - with the
mechanical mappings applied before validation, pointing at its llm_calls row. Append-only; empty at first.
llm_calls.http (Codex r192): each HTTP request's outcome; existing rows keep NULL (not known then)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_llm_outputs"
down_revision = "0017_upkeep_positions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_calls", sa.Column("http", postgresql.JSONB))
    op.create_table(
        "llm_outputs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("llm_call_id", sa.Integer, sa.ForeignKey("llm_calls.id"), nullable=False, index=True),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("mappings", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("llm_outputs")
    op.drop_column("llm_calls", "http")
