"""Drop the radar's side of Fabric's measurement objects (migration 0022): measurement_inbox,
measurement_withheld and series_points. 2026-11-16 the owner removed the measurement program from Fabric and
the radar - reading numbers out of notices into stored series was over-engineering (one source, 78 notices,
never run in production). An analysis now reads the cleaned source text, cites the sentence, and BioCheck
checks the number against it. The radar's own frozen tables (0023) are kept as they are.

Downgrading recreates nothing: run 0022's upgrade again if the tables are ever wanted back."""

from alembic import op

revision = "0026_drop_series_points"
down_revision = "0025_observation_relevance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TABLE IF EXISTS series_points")
    op.execute("DROP TABLE IF EXISTS measurement_withheld")
    op.execute("DROP TABLE IF EXISTS measurement_inbox")


def downgrade():
    pass
