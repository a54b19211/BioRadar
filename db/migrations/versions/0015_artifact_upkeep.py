"""reading plans and points per cleaned artifact, upkeep runs (T-26). A version's reading plan is one per
artifact (processor + text hash): an upgrade's plan replaces the old one, which is kept. A point carries the
artifact it was read from (from its candidate) and can be retired in favour of a reading of an upgraded
artifact. upkeep_runs / upkeep_items record operator-run, bounded upkeep (upgrade_reads,
backfill_corrections). Existing rows keep their state; artifact keys are filled from the columns already
stored (plans) and from the points' candidates."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_artifact_upkeep"
down_revision = "0014_measurement_jsonb_conflict"
branch_labels = None
depends_on = None

BASE = [
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
    "family",
    "dims_key",
    "value_text",
]


def upgrade():
    op.add_column(
        "record_reads", sa.Column("artifact_key", sa.String(200), nullable=False, server_default="")
    )
    op.add_column("record_reads", sa.Column("supersedes_id", sa.Integer))
    op.add_column("record_reads", sa.Column("superseded_by_id", sa.Integer))
    op.execute(
        "UPDATE record_reads SET artifact_key = processor_version || ':' || text_sha256 "
        "WHERE processor_version IS NOT NULL AND processor_version <> '' "
        "AND text_sha256 IS NOT NULL AND text_sha256 <> ''"
    )
    op.drop_constraint("record_reads_workspace_id_is_demo_version_id_key", "record_reads")
    op.create_unique_constraint(
        "record_reads_version_artifact_key",
        "record_reads",
        ["workspace_id", "is_demo", "version_id", "artifact_key"],
    )
    op.add_column(
        "measurements", sa.Column("artifact_key", sa.String(200), nullable=False, server_default="")
    )
    op.add_column("measurements", sa.Column("retired_by", sa.Integer))
    op.add_column("measurements", sa.Column("retired_reason", sa.String(40)))
    op.add_column("measurements", sa.Column("retired_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE measurements m SET artifact_key = c.processor_version || ':' || c.text_sha256 "
        "FROM measurement_candidates c WHERE c.id = m.candidate_id "
        "AND c.processor_version IS NOT NULL AND c.processor_version <> '' "
        "AND c.text_sha256 IS NOT NULL AND c.text_sha256 <> ''"
    )
    op.drop_constraint("measurements_version_series_period_domain_value_key", "measurements")
    op.create_unique_constraint(
        "measurements_version_series_period_domain_value_artifact_key",
        "measurements",
        BASE + ["artifact_key"],
    )
    op.create_table(
        "upkeep_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("params", postgresql.JSONB, nullable=False),
        sa.Column("cursor", sa.Integer, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("counts", postgresql.JSONB, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "upkeep_items",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("upkeep_runs.id"), nullable=False, index=True),
        sa.Column("key", sa.String(400), nullable=False),
        sa.Column("outcome", sa.String(60), nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    # the ORM sets these; no server defaults stay behind (alembic check compares them)
    for table in ("record_reads", "measurements"):
        op.alter_column(table, "artifact_key", server_default=None)


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
    # refused while upkeep runs, retirements or plan hand-overs are recorded (audit, Codex r193); the older
    # constraints could not hold a second artifact's plan or an upgraded reading of one value anyway
    _keep_audit(
        [
            ("SELECT count(*) FROM upkeep_runs", "upkeep runs"),
            ("SELECT count(*) FROM upkeep_items", "upkeep items"),
            ("SELECT count(*) FROM measurements WHERE retired_reason IS NOT NULL", "retired points"),
            (
                "SELECT count(*) FROM record_reads"
                " WHERE supersedes_id IS NOT NULL OR superseded_by_id IS NOT NULL",
                "plans handed over between artifacts",
            ),
        ]
    )
    op.drop_table("upkeep_items")
    op.drop_table("upkeep_runs")
    op.drop_constraint("measurements_version_series_period_domain_value_artifact_key", "measurements")
    op.create_unique_constraint("measurements_version_series_period_domain_value_key", "measurements", BASE)
    for column in ("retired_at", "retired_reason", "retired_by", "artifact_key"):
        op.drop_column("measurements", column)
    op.drop_constraint("record_reads_version_artifact_key", "record_reads")
    op.create_unique_constraint(
        "record_reads_workspace_id_is_demo_version_id_key",
        "record_reads",
        ["workspace_id", "is_demo", "version_id"],
    )
    for column in ("superseded_by_id", "supersedes_id", "artifact_key"):
        op.drop_column("record_reads", column)
