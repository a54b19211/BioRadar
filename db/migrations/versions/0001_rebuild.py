"""Rebuilt schema (2026-11). Starts from an empty database; the old database stays readable on its own."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_rebuild"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "topics",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("direction", sa.String(30), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("alert", sa.String(20), nullable=False),
        sa.Column("heat", sa.Float, nullable=False),
        sa.Column("awakenings", sa.Integer, nullable=False),
        sa.Column("closed", sa.Boolean, nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_activity_at", TS, nullable=False),
        sa.Column("last_analysis_id", sa.String(36)),
        sa.Column("summary", sa.Text, nullable=False),
    )
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("direction", sa.String(30), nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "investigation_id", sa.String(36), sa.ForeignKey("investigations.id"), nullable=False, index=True
        ),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("analyses.id")),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("method", sa.String(30), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("progress", JSONB, nullable=False),
        sa.Column("result", JSONB),
        sa.Column("error_code", sa.String(100)),
        sa.Column("lease_until", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("completed_at", TS),
        sa.UniqueConstraint("investigation_id", "input_hash"),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False, index=True),
        sa.Column("ref", sa.String(10), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("published_at", sa.String(40)),
        sa.Column("available_at", sa.String(40)),
        sa.Column("material_kind", sa.String(40)),
        sa.Column("verified", sa.Boolean, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("meta", JSONB, nullable=False),
        sa.UniqueConstraint("analysis_id", "ref"),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), index=True),
        sa.Column("stage", sa.String(60), nullable=False),
        sa.Column("service", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("finish_reason", sa.String(30)),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("seconds", sa.Float, nullable=False),
        sa.Column("error", sa.String(100)),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(400), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False, unique=True),
        sa.Column("is_demo", sa.Boolean, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("available_at", TS, nullable=False),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id"), index=True),
        sa.Column("alert", sa.String(20), nullable=False),
        sa.Column("closure", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
    )
    op.create_table(
        "topic_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id"), nullable=False, index=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
    )
    op.create_table(
        "monitor_cursors",
        sa.Column("source", sa.String(80), primary_key=True),
        sa.Column("is_demo", sa.Boolean, primary_key=True),
        sa.Column("after_seq", sa.Integer, nullable=False),
    )


def downgrade():
    for table in (
        "monitor_cursors",
        "topic_events",
        "observations",
        "llm_calls",
        "evidence",
        "analyses",
        "investigations",
        "topics",
    ):
        op.drop_table(table)
