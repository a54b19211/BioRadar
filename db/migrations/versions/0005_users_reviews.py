"""People who sign in, with roles, and their reviews of machine results (T-14)."""

import sqlalchemy as sa
from alembic import op

revision = "0005_users_reviews"
down_revision = "0004_measurements"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("credential_hash", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("session_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id")),
        sa.Column("sentence", sa.Integer),
        sa.Column("evidence_ref", sa.String(10)),
        sa.Column("topic_id", sa.String(36), sa.ForeignKey("topics.id")),
        sa.Column("alert", sa.String(20)),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reviews_analysis_id", "reviews", ["analysis_id"])
    op.create_index("ix_reviews_topic_id", "reviews", ["topic_id"])


def downgrade():
    op.drop_table("reviews")
    op.drop_table("users")
