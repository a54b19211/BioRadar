"""The radar's own measurement tables are frozen on PostgreSQL (migration 0023, docs/decisions/0010): every
insert, update, delete and truncate is refused - also below the ORM, also for a raw statement - reads go on,
and the radar's own new tables stay writable."""

import psycopg.errors
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from bioradar import db

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
CORRECTION = (
    "INSERT INTO record_corrections (workspace_id, is_demo, target_source, target_record_id, "
    "target_version_id, relation, by_source, by_record_id, by_version_id, visible_at, created_at) "
    "VALUES ('default', true, 'who_don', 'R1', '', 'corrects', 'who_don', 'R2', :v, now(), now())"
)


def refused(sql, **params):
    with pytest.raises(DBAPIError) as caught, db.engine().begin() as connection:
        connection.execute(text(sql), params)
    assert isinstance(caught.value.orig, psycopg.errors.ReadOnlySqlTransaction), caught.value
    assert "frozen read-only history" in str(caught.value.orig)


@pytest.fixture
def one_recorded_correction():
    """A row recorded before the freeze, put in the way a restore of old data would (triggers off)."""
    with db.engine().begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(text(CORRECTION), {"v": "a" * 64})
    yield


@pytest.mark.parametrize("table", FROZEN)
def test_no_row_can_be_added_to_a_frozen_table_nor_the_table_emptied(table):
    refused(f"INSERT INTO {table} DEFAULT VALUES")
    refused(f"TRUNCATE {table} CASCADE")


def test_a_recorded_row_cannot_be_changed_or_deleted_but_is_still_read(one_recorded_correction):
    refused("UPDATE record_corrections SET relation = 'retracts'")
    refused("DELETE FROM record_corrections")
    with db.session() as s:
        assert s.scalar(text("SELECT relation FROM record_corrections")) == "corrects"


def test_the_session_refuses_a_frozen_write_before_it_is_sent():
    with pytest.raises(db.FrozenHistory), db.session() as s:
        s.execute(text("SELECT 1"))
        s.add(
            db.RecordCorrection(
                workspace_id="default",
                is_demo=True,
                target_source="who_don",
                target_record_id="R1",
                relation="corrects",
                by_source="who_don",
                by_record_id="R2",
                by_version_id="b" * 64,
                visible_at=db.now(),
            )
        )


def test_the_radars_own_new_tables_stay_writable():
    with db.engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO monitor_cursors (source, is_demo, after_seq) VALUES ('frozen-test', true, 0)"
            )
        )
        connection.execute(text("DELETE FROM monitor_cursors WHERE source = 'frozen-test'"))
