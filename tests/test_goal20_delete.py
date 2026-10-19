"""(user 23:19): archived resumes can be deleted for good. × Delete for good (one) and Clear archived (all of
the person's) hide them at once and, after the Undo grace, delete the row, its local run files and its "Open as
Google Doc" copy, never the approved Drive PDF (the person's Sheet links it). Archived resumes older than
archived_delete_days (default 30, 0 = never) are deleted the same way once a day."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from resume_tailor import config, db, purge, server
from tests import test_goal5_edits as g5


def _stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeleteTests(unittest.TestCase):
    setUp, tearDown, call, _made = g5.ApiTests.setUp, g5.ApiTests.tearDown, g5.ApiTests.call, g5.ApiTests._made

    def archived(self, profile="alex-rivera", days_ago=1.0, **extra) -> int:
        jid = self._made(profile=profile)
        db.update(self.conn, jid, archived_at=_stamp(days_ago), **extra)
        return jid

    def listed(self, profile="alex-rivera") -> set:
        _, lean = self.call(f"/api/jobs?profile={profile}&lean=1")
        _, state = self.call(f"/api/state?profile={profile}")
        return {j["id"] for j in lean["jobs"]} | {j["id"] for j in state["archived"]}

    def test_delete_hides_at_once_then_purges_row_files_and_doc_never_the_drive_pdf(self):
        jid = self.archived(gdoc_id="DOC1", drive_file_id="PDF1")
        run = config.RUNS / str(jid)
        status, out = self.call(f"/api/jobs/{jid}/delete", {})
        self.assertEqual((status, out["deleting"]), (200, [jid]))
        self.assertNotIn(jid, self.listed())
        self.assertTrue(run.exists())                                    # kept during the Undo grace
        gone = []
        done = purge.due(self.conn, lambda pid, doc: gone.append((pid, doc)), grace_s=0)
        self.assertEqual(done, [jid])
        self.assertIsNone(db.get_job(self.conn, jid))
        self.assertFalse(run.exists())
        self.assertEqual(gone, [("alex-rivera", "DOC1")])                      # the Doc copy, never PDF1
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE job_id = ?", (jid,)).fetchone()[0], 0)

    def test_only_an_archived_resume_can_be_deleted(self):
        jid = self._made()
        status, out = self.call(f"/api/jobs/{jid}/delete", {})
        self.assertEqual(status, 400, out)
        self.assertIsNotNone(db.get_job(self.conn, jid))

    def test_undo_inside_the_grace_brings_it_back(self):
        jid = self.archived()
        self.call(f"/api/jobs/{jid}/delete", {})
        status, out = self.call("/api/undelete", {"ids": [jid]})
        self.assertEqual((status, out["restored"]), (200, [jid]))
        self.assertIn(jid, self.listed())
        self.assertEqual(purge.due(self.conn, lambda *a: None, grace_s=0), [])

    def test_clear_archived_takes_only_that_persons_archived(self):
        mine = [self.archived(), self.archived()]
        live = self._made()
        hers = self.archived(profile="sam-lee")
        status, out = self.call("/api/archived/clear", {"profile": "alex-rivera"})
        self.assertEqual((status, sorted(out["deleting"])), (200, sorted(mine)))
        self.assertEqual(sorted(purge.due(self.conn, lambda *a: None, grace_s=0)), sorted(mine))
        self.assertIsNotNone(db.get_job(self.conn, live))
        self.assertIsNotNone(db.get_job(self.conn, hers))

    def test_auto_clear_after_n_days_and_never_with_zero(self):
        old, young = self.archived(days_ago=31), self.archived(days_ago=29)
        self.assertEqual(purge.auto(self.conn, 0, lambda *a: None), [])
        self.assertEqual(purge.auto(self.conn, 30, lambda *a: None), [old])
        self.assertIsNone(db.get_job(self.conn, old))
        self.assertIsNotNone(db.get_job(self.conn, young))

    def test_auto_clear_leaves_a_just_pressed_delete_its_undo_time(self):
        """the daily run purged with no grace, so a × Delete pressed seconds before lost its Undo."""
        old, pressed = self.archived(days_ago=31), self.archived(days_ago=1)
        self.call(f"/api/jobs/{pressed}/delete", {})
        self.assertEqual(purge.auto(self.conn, 30, lambda *a: None), [old])
        self.assertIsNotNone(db.get_job(self.conn, pressed))
        status, out = self.call("/api/undelete", {"ids": [pressed]})
        self.assertEqual(out["restored"], [pressed])

    def test_the_setting_is_checked(self):
        self.assertEqual(server.validate_settings({"archived_delete_days": "45"}), {"archived_delete_days": 45})
        with self.assertRaises(ValueError):
            server.validate_settings({"archived_delete_days": -1})
        self.assertEqual(config.DEFAULTS["archived_delete_days"], 30)


if __name__ == "__main__":
    unittest.main()
