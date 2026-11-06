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


def _keep_audit(checks):
    """Refuse the downgrade (the run rolls back, version and rows kept) while any of these hold rows: audit is
    never deleted by an ordinary downgrade (Codex r193). checks: [(sql counting the rows, what they are)]."""
    bind = op.get_bind()
    held = [(bind.execute(sa.text(sql)).scalar() or 0, what) for sql, what in checks]
    held = [f"{n} {what}" for n, what in held if n]
    if held:
        raise RuntimeError(
            "downgrade refused, audit kept: " + "; ".join(held) + " - back up and settle first"
        )


def downgrade():
    _keep_audit(
        [("SELECT count(*) FROM upkeep_runs WHERE positions <> '{}'::jsonb", "upkeep runs with positions")]
    )
    op.drop_column("upkeep_runs", "positions")
