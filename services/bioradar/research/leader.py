"""One monitor at a time. The monitor loop (research.monitor.tick and analyses.recover) must not run in two
processes at once: both would triage the same records and re-queue the same analyses. The process that holds
the PostgreSQL advisory lock below is the leader; every other monitor process skips its rounds and tries
again at the next one.

The lock belongs to the session of one dedicated connection, so it is released by the database when the
holder exits or its connection breaks - there is no leader row that a crashed process leaves behind. A
database without advisory locks (SQLite in the unit tests) has one process by construction: always leader."""

from sqlalchemy import text

from bioradar import db, guard

# 64-bit key of the radar's monitor lock (the same on every host that shares the database)
MONITOR_LOCK = 0x62696F72_6D6F6E31


class Leader:
    def __init__(self, key: int = MONITOR_LOCK):
        self.key = key
        self._connection = None

    def acquire(self) -> bool:
        """Whether this process is the leader now: it holds the lock, taking it when it is free."""
        if db.engine().dialect.name != "postgresql":
            return True
        if self._connection is not None:
            try:
                self._connection.execute(text("SELECT 1"))  # the session, and so the lock, is still alive
                return True
            except Exception:  # noqa: BLE001 - the connection broke: the lock went with it
                self._drop()
        connection = db.engine().connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            if connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar():
                self._connection = connection
                return True
        except Exception:
            connection.close()
            raise
        connection.close()
        return False

    def assert_held(self):
        """guard.Revoked unless this process still holds the lock: the dedicated connection answers. A dropped
        connection took the lock with it, and whoever is leader now is not told about this process."""
        if db.engine().dialect.name != "postgresql":
            return
        if self._connection is None:
            raise guard.Revoked("monitor lock not held")
        try:
            self._connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - the connection broke: the lock went with it
            self._drop()
            raise guard.Revoked("monitor lock connection dropped") from None

    def release(self):
        """Give the lock up (a clean shutdown; a crash releases it just the same)."""
        if self._connection is not None:
            try:
                self._connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
            except Exception:  # noqa: BLE001 - already gone
                pass
            self._drop()

    def _drop(self):
        connection, self._connection = self._connection, None
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - broken connection
            pass
