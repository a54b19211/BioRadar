"""Long-term topic scopes, source-bound lightweight contributions and frozen reports."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_topics"
down_revision = "0006_monitor"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "topic_scopes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("topic_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("scope_revision", sa.Integer(), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("progress", postgresql.JSONB(), nullable=False),
        sa.Column("observation_revision", sa.Integer(), nullable=False),
        sa.Column("next_source", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("next_evaluation_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "topic_id", "scope_revision", name="uq_topic_scope_revision"),
    )
    op.create_index("ix_topic_scopes_topic_id", "topic_scopes", ["topic_id"])
    op.create_index("ix_topic_scopes_workspace_id", "topic_scopes", ["workspace_id"])
    op.create_index(
        "uq_topic_current",
        "topic_scopes",
        ["workspace_id", "topic_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "topic_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_id", sa.String(36), sa.ForeignKey("topic_scopes.id"), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("source_record_id", sa.String(1000), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("evidence_key", sa.String(64), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("withdrawn", sa.Boolean(), nullable=False),
        sa.Column("source_site", sa.String(200), nullable=False),
        sa.Column("publication_start", sa.DateTime(timezone=True)),
        sa.Column("publication_end", sa.DateTime(timezone=True)),
        sa.Column("metric_period_end", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_id", sa.String(100)),
        sa.Column("registration_status", sa.String(100)),
        sa.Column("dependency_source_id", sa.String(200)),
        sa.Column("supersedes_ref", postgresql.JSONB()),
        sa.Column("selected_from", sa.Integer()),
        sa.Column("selected_to", sa.Integer()),
        sa.Column("attention_pending", sa.Boolean(), nullable=False),
        sa.Column("contribution", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "scope_id", "source_id", "source_record_id", "version_id", name="uq_topic_source_version"
        ),
    )
    op.create_index("ix_topic_observations_scope_id", "topic_observations", ["scope_id"])
    op.create_index("ix_topic_evidence", "topic_observations", ["scope_id", "evidence_key"])
    op.create_index("ix_topic_supersedes", "topic_observations", ["supersedes_ref"], postgresql_using="gin")
    op.create_index("ix_topic_window", "topic_observations", ["scope_id", "publication_end"])
    op.create_index(
        "ix_topic_attention", "topic_observations", ["scope_id", "attention_pending", "selected_from"]
    )
    op.create_index(
        "uq_topic_selected",
        "topic_observations",
        ["scope_id", "source_id", "source_record_id"],
        unique=True,
        postgresql_where=sa.text("selected_from IS NOT NULL AND selected_to IS NULL"),
    )
    op.create_table(
        "topic_evaluations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_id", sa.String(36), sa.ForeignKey("topic_scopes.id"), nullable=False),
        sa.Column("evaluation_key", sa.String(64), nullable=False),
        sa.Column("observation_revision", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("followup", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scope_id", "evaluation_key", name="uq_topic_evaluation"),
    )
    op.create_index("ix_topic_evaluations_scope_id", "topic_evaluations", ["scope_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM topic_evaluations")):
        raise RuntimeError("Export saved topic evaluations before an explicit historical-data downgrade")
    op.drop_table("topic_evaluations")
    op.drop_table("topic_observations")
    op.drop_table("topic_scopes")
