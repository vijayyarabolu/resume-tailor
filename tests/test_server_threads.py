"""The server's handler threads used to share one SQLite connection. tx() locked writes, but reads ran unlocked
side by side, and the log showed `sqlite3.InterfaceError: bad parameter or other API misuse` on GET /api/state.
Each handler thread now has its own connection."""
import threading
import unittest
from unittest import mock

from resume_tailor import db, server
from tests.helpers import TempEnv

THREADS = 8
CALLS = 300


class ThreadConnectionTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.app = server.App()
        db.add_job(self.app.conn, "alex-rivera", "Acme", "ML Engineer", "Build models in Python. " * 10)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_each_thread_reads_on_its_own_connection(self):
        seen = {}

        def grab(n):
            seen[n] = id(self.app.conn)

        workers = [threading.Thread(target=grab, args=(n,)) for n in range(3)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(len(set(seen.values())), 3)
        self.assertIs(self.app.conn, self.app.conn)                      # the same one within a thread

    def test_concurrent_reads_never_misuse_the_connection(self):
        errors = []

        def hammer():
            try:
                for _ in range(CALLS):
                    db.pause_of(self.app.conn, "alex-rivera")
                    db.list_jobs(self.app.conn, "alex-rivera")
            except Exception as err:                                     # noqa: BLE001 - the test collects them
                errors.append(repr(err))

        workers = [threading.Thread(target=hammer) for _ in range(THREADS)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(errors, [])

    def test_the_google_helper_uses_the_calling_threads_connection(self):
        got = []
        worker = threading.Thread(target=lambda: got.append(self.app.google.conn.execute("SELECT 1").fetchone()[0]))
        worker.start()
        worker.join()
        self.assertEqual(got, [1])


class StartWarmsTheReadyListTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.app = server.App()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_start_builds_every_resume_once(self):
        # after a restart the first Resumes look took over 4 s reading every ready resume's files, and a tracker
        # with a 4 s timeout called the app offline; the server now reads them once as it starts. It builds the
        # whole lean list a tracker asks for (ready ones included), and an unchanged one is not rebuilt
        server._LEAN.clear()
        ready = db.add_job(self.app.conn, "alex-rivera", "Acme", "ML Engineer", "Build models in Python. " * 10)
        other = db.add_job(self.app.conn, "alex-rivera", "Beta", "Data Scientist", "Analyse data in SQL. " * 10)
        self.app.conn.execute("UPDATE jobs SET status = 'ready' WHERE id = ?", (ready,))
        real = server._job_view
        with mock.patch.object(server, "_job_view", side_effect=real) as view:
            server._warm()
            self.assertEqual(sorted(c.args[0]["id"] for c in view.call_args_list), [ready, other])
            server._warm()
        self.assertEqual(view.call_count, 2)


if __name__ == "__main__":
    unittest.main()
