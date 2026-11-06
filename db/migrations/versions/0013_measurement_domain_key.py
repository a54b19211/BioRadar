"""measurement domain key: one version may state the same base metric for two groups (two
products, two cohorts) - the point's identity includes its family and dimension group key. Existing points
keep theirs (family "" and the key 0012 left them), so no existing row changes."""

from alembic import op

revision = "0013_measurement_domain_key"
down_revision = "0012_measurement_dims"
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
]


def upgrade():
    op.drop_constraint("measurements_version_series_period_key", "measurements")
    op.create_unique_constraint(
        "measurements_version_series_period_domain_key", "measurements", BASE + ["family", "dims_key"]
    )


def downgrade():
    op.drop_constraint("measurements_version_series_period_domain_key", "measurements")
    op.create_unique_constraint("measurements_version_series_period_key", "measurements", BASE)
