"""llm_calls.lease_token: which execution of an analysis made a model call. The answer and cost of a request
that was sent before the worker lost its lease are still recorded, under the old worker's token, so they
count against the analysis's budget and stay in the audit. Existing rows have none."""

import sqlalchemy as sa
from alembic import op

revision = "0020_llm_call_lease_token"
down_revision = "0019_analysis_lease_token"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_calls", sa.Column("lease_token", sa.String(64)))


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
                "SELECT count(*) FROM llm_calls WHERE lease_token IS NOT NULL",
                "model calls recorded under a worker's lease token (llm_calls.lease_token)",
            )
        ]
    )
    op.drop_column("llm_calls", "lease_token")
