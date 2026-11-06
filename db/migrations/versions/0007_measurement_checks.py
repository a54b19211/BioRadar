"""measurement checks: candidates, check tasks, append-only checks; measurements bound to a check.

Existing measurements keep their rows and get check_id NULL: they are history, never in the effective view
(they were stored before any check and carry no literal texts to check now)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_measurement_checks"
down_revision = "0006_material_reads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "measurement_candidates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False, index=True),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("processor_version", sa.String(120)),
        sa.Column("text_sha256", sa.String(64)),
        sa.Column("quote_start", sa.Integer),
        sa.Column("quote_end", sa.Integer),
        sa.Column("measure", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(120)),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id"), index=True),
        sa.Column("direction", sa.String(30)),
        sa.Column("corrects", JSONB),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "measurement_check_tasks",
        sa.Column("candidate_id", sa.Integer, sa.ForeignKey("measurement_candidates.id"), primary_key=True),
        sa.Column("checker_version", sa.String(80), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error", JSONB),
    )
    op.create_table(
        "measurement_checks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "candidate_id", sa.Integer, sa.ForeignKey("measurement_candidates.id"), nullable=False, index=True
        ),
        sa.Column("checker_version", sa.String(80), nullable=False),
        sa.Column("contract_version", sa.String(40), nullable=False),
        sa.Column("request_sha256", sa.String(64)),
        sa.Column("request", JSONB),
        sa.Column("response", JSONB),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("gate", sa.String(20), nullable=False),
        sa.Column("gate_reason", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "CREATE RULE measurement_checks_no_update AS ON UPDATE TO measurement_checks DO INSTEAD NOTHING"
    )
    op.execute(
        "CREATE RULE measurement_checks_no_delete AS ON DELETE TO measurement_checks DO INSTEAD NOTHING"
    )
    for name, column in (
        ("candidate_id", sa.Integer),
        ("check_id", sa.Integer),
        ("value_text", sa.String(40)),
        ("denominator_text", sa.String(40)),
    ):
        op.add_column("measurements", sa.Column(name, column))
    for name, width in (("period_type", 10), ("period_precision", 10), ("scope_mode", 30)):
        op.add_column("measurements", sa.Column(name, sa.String(width), nullable=False, server_default=""))
    op.create_index("ix_measurements_candidate_id", "measurements", ["candidate_id"])
    op.create_index("ix_measurements_check_id", "measurements", ["check_id"])
    # 0004 left its unique constraint unnamed (PostgreSQL shortened the generated name): drop it by lookup
    for name in (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'measurements'::regclass AND contype = 'u'"
            )
        )
        .scalars()
    ):
        op.drop_constraint(name, "measurements")
    op.create_unique_constraint(
        "measurements_version_series_period_key",
        "measurements",
        [
            "version_id",
            "object",
            "metric",
            "unit",
            "kind",
            "scope",
            "scope_mode",
            "period",
            "period_type",
            "period_precision",
        ],
    )
    op.drop_index("ix_measurements_series", "measurements")
    op.create_index(
        "ix_measurements_series",
        "measurements",
        [
            "workspace_id",
            "is_demo",
            "object",
            "metric",
            "unit",
            "kind",
            "scope",
            "scope_mode",
            "period_type",
            "period_precision",
        ],
    )


def downgrade():
    op.drop_index("ix_measurements_series", "measurements")
    op.create_index(
        "ix_measurements_series",
        "measurements",
        ["workspace_id", "is_demo", "object", "metric", "unit", "kind", "scope"],
    )
    op.drop_constraint("measurements_version_series_period_key", "measurements")
    op.create_unique_constraint(
        "measurements_version_period_key_0004",
        "measurements",
        ["version_id", "object", "metric", "unit", "kind", "scope", "period"],
    )
    for name in ("ix_measurements_check_id", "ix_measurements_candidate_id"):
        op.drop_index(name, "measurements")
    for name in (
        "scope_mode",
        "period_precision",
        "period_type",
        "denominator_text",
        "value_text",
        "check_id",
        "candidate_id",
    ):
        op.drop_column("measurements", name)
    op.drop_table("measurement_checks")
    op.drop_table("measurement_check_tasks")
    op.drop_table("measurement_candidates")
