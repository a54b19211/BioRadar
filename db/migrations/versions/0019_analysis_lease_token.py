"""analyses.lease_token: the fencing token of an analysis lease, as check tasks and read windows have.
A worker writes progress and its result only while its token is the row's; recover releases the token.
Existing rows have none (an analysis running while this is applied has no token; it is fenced from its next
claim)."""

import sqlalchemy as sa
from alembic import op

revision = "0019_analysis_lease_token"
down_revision = "0018_llm_outputs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analyses", sa.Column("lease_token", sa.String(64)))


def _keep_audit(checks):
    """Refuse the downgrade (the run rolls back, version and rows kept) while any of these hold rows: audit is
    never deleted by an ordinary downgrade. checks: [(sql counting the rows, what they are)]."""
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
                "SELECT count(*) FROM analyses WHERE lease_token IS NOT NULL",
                "analyses held by a worker (lease_token)",
            )
        ]
    )
    op.drop_column("analyses", "lease_token")
