"""upkeep run positions (T-26, Codex r185/r186): where each pass of a run over items already met (retrying
what was not ready, settling, rechecking) resumes, so an item that stays unreadable never holds back the
others. Existing runs start their passes from the beginning."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_upkeep_positions"
down_revision = "0016_candidate_relocated_from"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "upkeep_runs",
        sa.Column("positions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.alter_column("upkeep_runs", "positions", server_default=None)  # the ORM sets it


def downgrade():
    op.drop_column("upkeep_runs", "positions")
