"""Emptying the tables of this run's throwaway test database between tests."""

from sqlalchemy import text


def truncate(connection, tables):
    """Empty `tables` with everything that refers to them. The frozen measurement tables refuse a TRUNCATE
    (migration 0023); in this run's own throwaway database they are emptied with the triggers off for this
    transaction only (session_replication_role = replica, a superuser setting)."""
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
