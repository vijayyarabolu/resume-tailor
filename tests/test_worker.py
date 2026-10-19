"""The worker loop with a fake pipeline: rate-limit pause/resume (nothing lost), missing-login wait, cancel,
and the socket wake. Claude is never called. The CLI backend is used so each person can have their own login."""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from resume_tailor import config, db, pipeline, profiles, worker
from tests.helpers import JD_TEXT, TempEnv

LOGGED_IN = {"logged_in": True, "email": "p@example.com", "subscription": "max"}
LOGGED_OUT = {"logged_in": False, "email": None, "subscription": None}


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv({"login_recheck_min": 0.02, "concurrency": 1, "claude_backend": "cli"}).__enter__()
        toml = config.PROFILES / "sam-lee" / "profile.toml"
        toml.write_text('claude_config_dir = "~/.claude-sam"\n' + toml.read_text())
        profiles.reload()
        self.conn = db.connect()
        self.accounts = {"alex-rivera": LOGGED_IN, "sam-lee": LOGGED_IN}
        self.status_patch = mock.patch("resume_tailor.claude_runner.account_status",
                                       side_effect=lambda cfg, _bin="claude": self.accounts["sam-lee" if cfg else "alex-rivera"])
        self.status_patch.start()
        self.google_patch = mock.patch("resume_tailor.gdocs.Google", return_value=None)
        self.google_patch.start()
        self.w = None
        self.thread = None

    def tearDown(self):
        if self.w:
            self.w.stop.set()
            worker.wake()
            self.thread.join(timeout=5)
        self.google_patch.stop()
        self.status_patch.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def start(self):
        self.w = worker.Worker()
        self.thread = threading.Thread(target=self.w.main, daemon=True)
        self.thread.start()
        self.assertTrue(_wait(lambda: config.SOCK_PATH.exists()))

    def add(self, profile="alex-rivera", company="Acme"):
        return db.add_job(self.conn, profile, company, "ML Engineer", JD_TEXT)

    def statuses(self, ids):
        return [db.get_job(self.conn, i)["status"] for i in ids]

    def test_usage_limit_pauses_then_resumes_without_losing_jobs(self):
        calls = []

        def fake_process(conn, job, settings, google, runner=None):
            calls.append(job["id"])
            if len(calls) == 1:
                raise pipeline.Limit(datetime.now(timezone.utc) + timedelta(seconds=2), "usage limit reached")
            db.set_status(conn, job["id"], "ready", "ready")
            return "ready"

        with mock.patch.object(pipeline, "process", side_effect=fake_process):
            a, b = self.add(), self.add(company="Beta")
            self.start()
            worker.wake()
            self.assertTrue(_wait(lambda: db.pause_of(self.conn, "alex-rivera") is not None
                                  and self.statuses([a, b]) == ["queued", "queued"], 5))
            pause = db.pause_of(self.conn, "alex-rivera")
            self.assertIn("usage limit", pause["reason"])
            self.assertEqual(self.statuses([a, b]), ["queued", "queued"])         # nothing dropped
            self.assertTrue(_wait(lambda: self.statuses([a, b]) == ["ready", "ready"], 15))
            self.assertIsNone(db.pause_of(self.conn, "alex-rivera"))
            self.assertEqual(calls, [a, a, b])                                    # a retried first, then b
            texts = [e["text"] for e in db.events_since(self.conn, 0) if e["kind"] == "toast"]
            self.assertTrue(any("Paused until" in t for t in texts))

    def test_missing_login_parks_jobs_until_login_appears(self):
        def fake_process(conn, job, settings, google, runner=None):
            db.set_status(conn, job["id"], "ready", "ready")
            return "ready"

        self.accounts["sam-lee"] = LOGGED_OUT
        with mock.patch.object(pipeline, "process", side_effect=fake_process):
            s = self.add(profile="sam-lee")
            v = self.add()
            self.start()
            worker.wake()
            self.assertTrue(_wait(lambda: self.statuses([s, v]) == ["waiting", "ready"], 10))
            job = db.get_job(self.conn, s)
            self.assertIn("Claude Code is not logged in", job["error"])
            self.assertIn("CLAUDE_CONFIG_DIR=~/.claude-sam claude auth login", job["error"])
            self.accounts["sam-lee"] = LOGGED_IN
            worker.wake()
            self.assertTrue(_wait(lambda: self.statuses([s]) == ["ready"], 10))

    def test_job_exception_marks_failed_and_worker_survives(self):
        def fake_process(conn, job, settings, google, runner=None):
            if job["company"] == "Boom":
                raise RuntimeError("kaboom")
            db.set_status(conn, job["id"], "ready", "ready")
            return "ready"

        with mock.patch.object(pipeline, "process", side_effect=fake_process):
            a, b = self.add(company="Boom"), self.add(company="Fine")
            self.start()
            worker.wake()
            self.assertTrue(_wait(lambda: self.statuses([a, b]) == ["failed", "ready"], 10))
            self.assertIn("kaboom", db.get_job(self.conn, a)["error"])

    def test_cancelled_pipeline_sets_cancelled(self):
        def fake_process(conn, job, settings, google, runner=None):
            raise pipeline.Cancelled()

        with mock.patch.object(pipeline, "process", side_effect=fake_process):
            a = self.add()
            self.start()
            worker.wake()
            self.assertTrue(_wait(lambda: self.statuses([a]) == ["cancelled"], 10))

    def test_wake_without_worker_is_false(self):
        self.assertFalse(worker.wake())

    def test_stale_jobs_recovered_at_start(self):
        def fake_process(conn, job, settings, google, runner=None):
            db.set_status(conn, job["id"], "ready", "ready")
            return "ready"

        with mock.patch.object(pipeline, "process", side_effect=fake_process):
            a = self.add()
            db.claim(self.conn, a, "/tmp/x")                       # a worker died here
            self.start()
            self.assertTrue(_wait(lambda: self.statuses([a]) == ["ready"], 10))


if __name__ == "__main__":
    unittest.main()
