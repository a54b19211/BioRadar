"""MechEvidence evaluations (v2 contract): the reusable result cache, the receipts that were not adopted,
and the authorization / attempt ledger the radar keeps for the model requests a service makes.

mech_evaluations / mech_evaluation_uses: a complete evaluation by its reuse key and which analyses used it
(computed or reused). mech_receipts_rejected: a 200 receipt that was not adopted, whole. model_attempt_
authorizations / model_attempts: what an evaluation may send (analysis, execution, evaluation key, evaluator,
materials, expiry, request allowance) and each request the radar allowed (prepared), refused or was told the
outcome of (settled). llm_outputs: a request without an answer has no content, and a service's request carries
its ledger attempt id, delivery state, HTTP status and truncation flag. llm_calls: whether the usage is the
provider's or "not known" (zeros), and the authorization a service's requests ran under. Existing rows are
unchanged (usage_known true; the new columns empty)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_mech_attempt_ledger"
down_revision = "0020_llm_call_lease_token"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "model_attempt_authorizations",
        sa.Column("ref", sa.String(64), primary_key=True),
        sa.Column("service", sa.String(30), nullable=False),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False, index=True),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("evaluation_key", sa.String(64), nullable=False),
        sa.Column("evaluator_id", sa.String(64), nullable=False),
        sa.Column("evaluator", postgresql.JSONB, nullable=False),
        sa.Column("materials", postgresql.JSONB, nullable=False),
        sa.Column("sources", postgresql.JSONB, nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("mode", sa.String(40), nullable=False),
        sa.Column("cutoff", TS),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "model_attempts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "authorization_ref",
            sa.String(64),
            sa.ForeignKey("model_attempt_authorizations.ref"),
            nullable=False,
            index=True,
        ),
        sa.Column("attempt_id", sa.String(64), nullable=False, unique=True),
        sa.Column("decision_id", sa.String(64), unique=True),
        sa.Column("idx", sa.Integer, nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("request_body_sha256", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("endpoint_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(60)),
        sa.Column("decided_at", TS, nullable=False),
        sa.Column("expires_at", TS),
        sa.Column("delivered", sa.String(10)),
        sa.Column("http_status", sa.Integer),
        sa.Column("error", sa.String(200)),
        sa.Column("response_sha256", sa.String(64)),
        sa.Column("truncated", sa.Boolean),
        sa.Column("response_text", sa.Text),
        sa.Column("settled_at", TS),
        sa.UniqueConstraint("authorization_ref", "idx"),
    )
    op.create_table(
        "mech_evaluations",
        sa.Column("reuse_key", sa.String(64), primary_key=True),
        sa.Column("contract_version", sa.String(60), nullable=False),
        sa.Column("evaluator_id", sa.String(64), nullable=False, index=True),
        sa.Column("evaluator", postgresql.JSONB, nullable=False),
        sa.Column("proposition", postgresql.JSONB, nullable=False),
        sa.Column("scope", postgresql.JSONB, nullable=False),
        sa.Column("materials", postgresql.JSONB, nullable=False),
        sa.Column("sources", postgresql.JSONB, nullable=False),
        sa.Column("response", postgresql.JSONB, nullable=False),
        sa.Column("first_analysis_id", sa.String(36), sa.ForeignKey("analyses.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "mech_evaluation_uses",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False, index=True),
        sa.Column(
            "reuse_key",
            sa.String(64),
            sa.ForeignKey("mech_evaluations.reuse_key"),
            nullable=False,
            index=True,
        ),
        sa.Column("how", sa.String(10), nullable=False),
        sa.Column("used_at", TS, nullable=False),
    )
    op.create_table(
        "mech_receipts_rejected",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False, index=True),
        sa.Column("authorization_ref", sa.String(64), index=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("response", postgresql.JSONB, nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=False),
        sa.Column("received_at", TS, nullable=False),
    )
    op.alter_column("llm_outputs", "content_sha256", existing_type=sa.String(64), nullable=True)
    op.alter_column("llm_outputs", "content", existing_type=sa.Text, nullable=True)
    op.add_column("llm_outputs", sa.Column("attempt_id", sa.String(64)))
    op.create_index("ix_llm_outputs_attempt_id", "llm_outputs", ["attempt_id"])
    op.add_column("llm_outputs", sa.Column("delivered", sa.String(10)))
    op.add_column("llm_outputs", sa.Column("http_status", sa.Integer))
    op.add_column("llm_outputs", sa.Column("truncated", sa.Boolean))
    op.add_column("llm_calls", sa.Column("usage_known", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("llm_calls", sa.Column("authorization_ref", sa.String(64)))
    op.create_index("ix_llm_calls_authorization_ref", "llm_calls", ["authorization_ref"])


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
            ("SELECT count(*) FROM mech_evaluations", "MechEvidence evaluations (mech_evaluations)"),
            ("SELECT count(*) FROM mech_evaluation_uses", "uses of those (mech_evaluation_uses)"),
            ("SELECT count(*) FROM mech_receipts_rejected", "receipts not adopted (mech_receipts_rejected)"),
            ("SELECT count(*) FROM model_attempt_authorizations", "model request authorizations"),
            ("SELECT count(*) FROM model_attempts", "model requests of a service (model_attempts)"),
            (
                "SELECT count(*) FROM llm_outputs WHERE attempt_id IS NOT NULL OR content IS NULL",
                "service model answers kept (llm_outputs with an attempt id, or without content)",
            ),
            (
                "SELECT count(*) FROM llm_calls WHERE authorization_ref IS NOT NULL OR usage_known = false",
                "model calls under an authorization or with unknown usage (llm_calls)",
            ),
        ]
    )
    op.drop_index("ix_llm_calls_authorization_ref", table_name="llm_calls")
    op.drop_column("llm_calls", "authorization_ref")
    op.drop_column("llm_calls", "usage_known")
    op.drop_column("llm_outputs", "truncated")
    op.drop_column("llm_outputs", "http_status")
    op.drop_column("llm_outputs", "delivered")
    op.drop_index("ix_llm_outputs_attempt_id", table_name="llm_outputs")
    op.drop_column("llm_outputs", "attempt_id")
    op.alter_column("llm_outputs", "content", existing_type=sa.Text, nullable=False)
    op.alter_column("llm_outputs", "content_sha256", existing_type=sa.String(64), nullable=False)
    for table in (
        "mech_receipts_rejected",
        "mech_evaluation_uses",
        "mech_evaluations",
        "model_attempts",
        "model_attempt_authorizations",
    ):
        op.drop_table(table)
