"""Manual investigations are saved records, outside the two analysis run kinds."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_investigations"
down_revision = "0003_decision"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("question", sa.String(4000), nullable=False),
        sa.Column("origin", postgresql.JSONB(), nullable=True),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('open', 'paused', 'closed')", name="ck_investigation_status"),
        sa.CheckConstraint("revision >= 1", name="ck_investigation_revision"),
    )
    op.create_index("ix_investigations_workspace_id", "investigations", ["workspace_id"])
    op.create_table(
        "investigation_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("investigation_id", sa.String(36), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("investigation_id", "revision"),
        sa.CheckConstraint(
            "kind IN ('material', 'note', 'judgment', 'status')", name="ck_investigation_entry_kind"
        ),
        sa.CheckConstraint("revision >= 2", name="ck_investigation_entry_revision"),
    )
    op.create_index(
        "ix_investigation_entries_investigation_id", "investigation_entries", ["investigation_id"]
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM investigations")):
        raise RuntimeError("Cannot discard saved investigation history when downgrading")
    op.drop_index("ix_investigation_entries_investigation_id", table_name="investigation_entries")
    op.drop_table("investigation_entries")
    op.drop_index("ix_investigations_workspace_id", table_name="investigations")
    op.drop_table("investigations")
