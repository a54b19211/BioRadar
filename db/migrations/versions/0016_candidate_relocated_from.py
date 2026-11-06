"""measurement candidates relocated from an older artifact (T-26): a known fact of an older artifact of the
version located again in an upgraded artifact by its quote names the candidate it came from. Existing rows
have none."""

import sqlalchemy as sa
from alembic import op

revision = "0016_candidate_relocated_from"
down_revision = "0015_artifact_upkeep"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("measurement_candidates", sa.Column("relocated_from", sa.Integer))
    op.create_index("ix_measurement_candidates_relocated_from", "measurement_candidates", ["relocated_from"])


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
        [
            (
                "SELECT count(*) FROM measurement_candidates WHERE relocated_from IS NOT NULL",
                "relocated candidates (relocated_from)",
            )
        ]
    )
    op.drop_index("ix_measurement_candidates_relocated_from", "measurement_candidates")
    op.drop_column("measurement_candidates", "relocated_from")
