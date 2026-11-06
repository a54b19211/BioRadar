"""measurement JSONB and record conflict. dims / dims_reasons become JSONB on PostgreSQL,
as
the model declares them (0012 declared JSON). A point's stored identity includes its value: one record version
stating one identity with two values keeps both (as a contradiction, never compared) - the group key is
unchanged. Existing rows change in type only."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_measurement_jsonb_conflict"
down_revision = "0013_measurement_domain_key"
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
]


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        for column in ("dims", "dims_reasons"):
            op.alter_column(
                "measurements",
                column,
                type_=postgresql.JSONB(),
                existing_type=sa.JSON(),
                postgresql_using=f"{column}::jsonb",
            )
    op.drop_constraint("measurements_version_series_period_domain_key", "measurements")
    op.create_unique_constraint(
        "measurements_version_series_period_domain_value_key", "measurements", BASE + ["value_text"]
    )


def downgrade():
    # fails (and rolls back) once a record contradiction is stored: two values of one identity cannot fit the
    # older constraint - back up and resolve them first (handover note)
    op.drop_constraint("measurements_version_series_period_domain_value_key", "measurements")
    op.create_unique_constraint("measurements_version_series_period_domain_key", "measurements", BASE)
    if op.get_bind().dialect.name == "postgresql":
        for column in ("dims", "dims_reasons"):
            op.alter_column(
                "measurements",
                column,
                type_=sa.JSON(),
                existing_type=postgresql.JSONB(),
                postgresql_using=f"{column}::json",
            )
