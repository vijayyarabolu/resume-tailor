"""The DB connections: busy_timeout first, WAL set only on a file not in WAL yet, synchronous
NORMAL, the WAL capped, the schema set up once per process and file, a lock at connect tried again."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from resume_tailor import db


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "resume-tailor.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _traced(self):
        seen, real = [], sqlite3.connect

        def spy(*a, **k):
            conn = real(*a, **k)
            conn.set_trace_callback(seen.append)
            return conn
        return seen, mock.patch.object(db.sqlite3, "connect", spy)

    def test_settings_and_one_schema_setup_per_file(self):
        db.connect(self.path).close()
        seen, spy = self._traced()
        with spy:
            conn = db.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA journal_size_limit").fetchone()[0], db.WAL_LIMIT_BYTES)
        self.assertNotIn("PRAGMA journal_mode=WAL", seen)
        self.assertFalse(any(s.startswith("CREATE TABLE") for s in seen), seen)     # set up already
        self.assertLess(seen.index(f"PRAGMA busy_timeout={db.BUSY_TIMEOUT_MS}"), seen.index("PRAGMA journal_mode"))
        conn.close()

    def test_a_new_file_at_the_same_path_is_set_up_again(self):
        db.connect(self.path).close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        conn = db.connect(self.path)
        conn.execute("SELECT count(*) FROM jobs").fetchone()
        conn.close()

    def test_a_lock_at_connect_is_tried_again(self):
        calls, real = [], db._open

        def flaky(path):
            calls.append(path)
            if len(calls) < 2:
                raise sqlite3.OperationalError("database is locked")
            return real(path)
        with mock.patch.object(db, "_open", flaky), mock.patch.object(db, "CONNECT_TRIES", (0.01, 0.01)):
            db.connect(self.path).close()
        self.assertEqual(len(calls), 2)
