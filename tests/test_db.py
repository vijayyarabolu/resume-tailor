import unittest
from datetime import datetime, timedelta, timezone

from resume_tailor import db
from tests.helpers import JD_TEXT, TempEnv


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        self.env.__exit__(None, None, None)

    def add(self, profile="alex-rivera", company="Acme", role="ML Engineer"):
        return db.add_job(self.conn, profile, company, role, JD_TEXT, "https://example.com/j/1", "ex:1")

    def test_add_job_is_queued_with_event(self):
        jid = self.add()
        job = db.get_job(self.conn, jid)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(job["tracker_job_id"], "ex:1")
        events = db.events_since(self.conn, 0)
        self.assertEqual(events[-1]["job_id"], jid)
        self.assertEqual(events[-1]["data"]["status"], "queued")

    def test_next_queued_is_fifo_per_profile(self):
        a, b = self.add(), self.add(company="Beta")
        s = self.add(profile="sam-lee")
        self.assertEqual(db.next_queued(self.conn, "alex-rivera")["id"], a)
        self.assertEqual(db.next_queued(self.conn, "sam-lee")["id"], s)
        self.assertTrue(db.claim(self.conn, a, "/tmp/x"))
        self.assertEqual(db.next_queued(self.conn, "alex-rivera")["id"], b)

    def test_move_to_top_runs_next_and_keeps_fifo_for_the_rest(self):
        a, b, c = self.add(), self.add(company="Beta"), self.add(company="Gamma")
        s = self.add(profile="sam-lee")
        db.move_to_top(self.conn, c)
        self.assertEqual(db.next_queued(self.conn, "alex-rivera")["id"], c)
        self.assertEqual([j["id"] for j in db.queue_order(self.conn, "alex-rivera")], [c, a, b])
        db.move_to_top(self.conn, b)
        self.assertEqual([j["id"] for j in db.queue_order(self.conn, "alex-rivera")], [b, c, a])
        self.assertEqual(db.next_queued(self.conn, "sam-lee")["id"], s)      # another person's queue is untouched
        db.claim(self.conn, b, "/tmp/x")
        self.assertEqual(db.next_queued(self.conn, "alex-rivera")["id"], c)

    def test_move_to_top_only_for_a_waiting_job(self):
        a = self.add()
        db.claim(self.conn, a, "/tmp/x")
        with self.assertRaises(ValueError):
            db.move_to_top(self.conn, a)
        with self.assertRaises(KeyError):
            db.move_to_top(self.conn, 999999)

    def test_claim_only_once(self):
        jid = self.add()
        self.assertTrue(db.claim(self.conn, jid, "/tmp/run"))
        self.assertFalse(db.claim(self.conn, jid, "/tmp/run"))
        job = db.get_job(self.conn, jid)
        self.assertEqual(job["status"], "generating")
        self.assertIsNotNone(job["started_at"])

    def test_terminal_status_records_elapsed(self):
        jid = self.add()
        db.claim(self.conn, jid, "/tmp/run")
        db.set_status(self.conn, jid, "ready", "ready after 1 attempt")
        job = db.get_job(self.conn, jid)
        self.assertEqual(job["status"], "ready")
        self.assertIsNotNone(job["finished_at"])
        self.assertGreaterEqual(job["elapsed_s"], 0)

    def test_requeue_keeps_note_and_resets_attempts(self):
        jid = self.add()
        db.claim(self.conn, jid, "/tmp/run")
        db.update(self.conn, jid, attempts=3)
        db.set_status(self.conn, jid, "needs_you", "needs you", error="widow")
        db.requeue(self.conn, jid, note="more MLOps", base_override="ai_engineer")
        job = db.get_job(self.conn, jid)
        self.assertEqual((job["status"], job["attempts"], job["note"], job["base_override"], job["error"]),
                         ("queued", 0, "more MLOps", "ai_engineer", None))
        db.claim(self.conn, jid, "/tmp/run")
        db.update(self.conn, jid, attempts=2)
        db.requeue(self.conn, jid, keep_attempts=True)
        self.assertEqual(db.get_job(self.conn, jid)["attempts"], 2)

    def test_cancel_queued_and_running(self):
        a, b = self.add(), self.add(company="Beta")
        self.assertEqual(db.cancel(self.conn, a), "cancelled")
        db.claim(self.conn, b, "/tmp/run")
        self.assertEqual(db.cancel(self.conn, b), "cancel_requested")
        self.assertTrue(db.cancel_requested(self.conn, b))
        self.assertEqual(db.get_job(self.conn, b)["status"], "generating")

    def test_recover_stale_requeues_running_jobs(self):
        jid = self.add()
        db.claim(self.conn, jid, "/tmp/run")
        self.assertEqual(db.recover_stale(self.conn), 1)
        self.assertEqual(db.get_job(self.conn, jid)["status"], "queued")

    def test_pause_expires_automatically(self):
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        db.set_pause(self.conn, "alex-rivera", until, "usage limit")
        pause = db.pause_of(self.conn, "alex-rivera")
        self.assertEqual(pause["reason"], "usage limit")
        self.assertIsNone(db.pause_of(self.conn, "sam-lee"))
        db.set_pause(self.conn, "alex-rivera", datetime.now(timezone.utc) - timedelta(seconds=1), "old")
        self.assertIsNone(db.pause_of(self.conn, "alex-rivera"))

    def test_events_since_and_last_id(self):
        self.add()
        self.add(company="Beta")
        last = db.last_event_id(self.conn)
        self.assertEqual(db.events_since(self.conn, last), [])
        self.assertEqual(len(db.events_since(self.conn, last - 1)), 1)

    def test_parse_ts_round_trip(self):
        stamp = db.now()
        parsed = db.parse_ts(stamp)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), stamp)

    def test_result_of_handles_bad_json(self):
        jid = self.add()
        db.update(self.conn, jid, result="{not json")
        self.assertEqual(db.result_of(db.get_job(self.conn, jid)), {})


if __name__ == "__main__":
    unittest.main()
