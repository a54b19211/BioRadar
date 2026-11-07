"""The radar's own measurement chain from before Fabric took it over is frozen history
(docs/decisions/0010-fabric-measurements.md): PostgreSQL refuses every insert, update, delete and truncate on
its tables. The rows are kept as they are; nothing is moved or deleted. Downgrading lifts the freeze only."""

from alembic import op

revision = "0023_freeze_measurement_history"
down_revision = "0022_fabric_measurements"
branch_labels = None
depends_on = None

FROZEN = (
    "measurements",
    "measurement_candidates",
    "measurement_check_tasks",
    "measurement_checks",
    "record_reads",
    "record_read_windows",
    "record_read_attempts",
    "record_corrections",
)


def upgrade():
    op.execute(
        """
        CREATE FUNCTION bioradar_frozen_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is frozen read-only history (docs/decisions/0010)', TG_TABLE_NAME
                USING ERRCODE = 'read_only_sql_transaction';
        END
        $$
        """
    )
    for table in FROZEN:
        op.execute(
            f"CREATE TRIGGER {table}_frozen BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bioradar_frozen_history()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_frozen_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION bioradar_frozen_history()"
        )


def downgrade():
    for table in FROZEN:
        op.execute(f"DROP TRIGGER {table}_frozen_truncate ON {table}")
        op.execute(f"DROP TRIGGER {table}_frozen ON {table}")
    op.execute("DROP FUNCTION bioradar_frozen_history()")
