"""review and clear the queue. Looks good (accept), Fix with note (one focused repair), Archive /
Clear cancelled & failed / Archive all approved, the 24 h auto-archive, plain-words reasons, and the API."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from resume_tailor import config, db, docxio, pdfx, pipeline, reasons, render, server
from tests.helpers import JD_TEXT, TempEnv, example_profile
from tests.test_pipeline import _ok_result

TOOLS = not pdfx.tools_missing()
NO_FIX = {"bullets": [], "skills": [], "summary": None, "notes": []}


def _needs_you(conn, pdf="/tmp/x.pdf", failures=None) -> int:
    jid = db.add_job(conn, "alex-rivera", "Acme", "Machine Learning Engineer", JD_TEXT)
    failures = failures or [{"check": 7, "name": "bullet_words", "message": "bullet 5 of role 1 (Lumen Freight, "
                                                                          "USA) has 26 words; the rule is 15 to 25"}]
    db.update(conn, jid, attempts=2, result=json.dumps({"files": {"pdf": pdf}, "checks": {"ok": False,
                                                                                          "failures": failures}}))
    db.set_status(conn, jid, "needs_you", error="bullet 5 of role 1 has 26 words")
    return jid


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReviewDbTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        self.env.__exit__(None, None, None)

    def test_looks_good_makes_it_ready_and_records_the_reasons(self):
        jid = _needs_you(self.conn)
        despite = db.accept(self.conn, jid)
        row = db.get_job(self.conn, jid)
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["error"])
        self.assertEqual(db.result_of(row)["accepted"]["despite"], despite)
        self.assertIn("26 words", despite[0])
        events = [e["text"] for e in db.events_since(self.conn, 0)]
        self.assertTrue(any(t.startswith("accepted by user despite: ") for t in events))
        with self.assertRaises(ValueError):
            db.accept(self.conn, jid)                         # only a needs-you resume

    def test_accept_needs_a_resume(self):
        jid = _needs_you(self.conn, pdf=None)
        with self.assertRaises(ValueError):
            db.accept(self.conn, jid)

    def test_fix_with_note_queues_with_rounds_kept_and_survives_a_pause_requeue(self):
        jid = _needs_you(self.conn)
        with self.assertRaises(ValueError):
            db.fix_with_note(self.conn, jid, "   ")
        db.fix_with_note(self.conn, jid, "only make the summary shorter")
        row = db.get_job(self.conn, jid)
        self.assertEqual((row["status"], row["attempts"], row["fix_note"]), ("queued", 2,
                                                                            "only make the summary shorter"))
        db.claim(self.conn, jid, "x")
        db.requeue(self.conn, jid, keep_attempts=True)        # what the worker does on a usage limit
        self.assertEqual(db.get_job(self.conn, jid)["fix_note"], "only make the summary shorter")
        db.set_status(self.conn, jid, "failed")
        db.requeue(self.conn, jid, note="start over")          # a plain retry drops the pending fix
        self.assertIsNone(db.get_job(self.conn, jid)["fix_note"])

    def test_archive_hides_finished_jobs_but_never_live_or_needs_you(self):
        ready = db.add_job(self.conn, "alex-rivera", "A", "R", JD_TEXT)
        db.set_status(self.conn, ready, "ready")
        live = db.add_job(self.conn, "alex-rivera", "B", "R", JD_TEXT)
        nyou = _needs_you(self.conn)
        db.archive(self.conn, ready)
        for jid in (live, nyou):
            with self.assertRaises(ValueError):
                db.archive(self.conn, jid)
        visible = {j["id"] for j in db.list_jobs(self.conn, include_archived=False)}
        self.assertEqual(visible, {live, nyou})
        self.assertIn(ready, {j["id"] for j in db.list_jobs(self.conn)})       # a tracker's listing keeps it
        self.assertTrue(Path(config.DB_PATH).exists())

    def test_clear_cancelled_and_failed_and_archive_all_approved(self):
        c = db.add_job(self.conn, "alex-rivera", "ZZ_TEST Co", "R", JD_TEXT)
        db.cancel(self.conn, c)
        f = db.add_job(self.conn, "sam-lee", "F", "R", JD_TEXT)
        db.set_status(self.conn, f, "failed", error="PDF conversion failed")
        a = db.add_job(self.conn, "alex-rivera", "A", "R", JD_TEXT)
        db.set_status(self.conn, a, "ready")
        db.update(self.conn, a, drive_file_id="drive1")
        r = db.add_job(self.conn, "alex-rivera", "R", "R", JD_TEXT)
        db.set_status(self.conn, r, "ready")
        self.assertEqual(sorted(db.archive_many(self.conn, "cancelled_failed")), [c, f])
        self.assertEqual(db.archive_many(self.conn, "approved"), [a])
        mine = self.conn.execute("SELECT text FROM events WHERE job_id = ? ORDER BY id DESC", (a,)).fetchone()
        self.assertEqual(mine["text"], "archived (Archive all approved)")     # each job's history shows a batch
        self.assertEqual(db.archive_many(self.conn, "approved"), [])
        self.assertEqual({j["id"] for j in db.list_jobs(self.conn, include_archived=False)}, {r})
        with self.assertRaises(ValueError):
            db.archive_many(self.conn, "everything")
        db.requeue(self.conn, c)                               # a retried job comes back into view
        self.assertIsNone(db.get_job(self.conn, c)["archived_at"])

    def test_restore_brings_single_and_batch_archives_back_unchanged(self):
        a = db.add_job(self.conn, "alex-rivera", "A", "R", JD_TEXT)
        db.set_status(self.conn, a, "ready")
        db.update(self.conn, a, drive_file_id="drive1", drive_link="https://drive/1", result=json.dumps({"files": {"pdf": "/p"}}))
        c = db.add_job(self.conn, "alex-rivera", "C", "R", JD_TEXT)
        db.cancel(self.conn, c)
        before = {k: v for k, v in db.get_job(self.conn, a).items() if k not in ("archived_at", "updated_at",
                                                                               "restored_at", "rev")}   # rev: the lean-list counter
        db.archive(self.conn, a)
        batch = db.archive_many(self.conn, "cancelled_failed")
        self.assertEqual([j["id"] for j in db.archived(self.conn)], [c, a] if batch else [a])
        self.assertEqual(db.unarchive(self.conn, [a]), [a])
        after = {k: v for k, v in db.get_job(self.conn, a).items() if k not in ("archived_at", "updated_at",
                                                                              "restored_at", "rev")}
        self.assertEqual(after, before)                        # status, files, Drive link: all as they were
        self.assertEqual(db.unarchive(self.conn, batch + [999]), batch)
        self.assertEqual(db.archived(self.conn), [])

    def test_a_restored_old_cancelled_job_is_not_auto_archived_again(self):
        c = db.add_job(self.conn, "alex-rivera", "Old", "R", JD_TEXT)
        db.cancel(self.conn, c)
        self.conn.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (_ago(48), c))
        self.assertEqual(db.auto_archive(self.conn), [c])
        db.unarchive(self.conn, [c])
        self.assertEqual(db.auto_archive(self.conn), [])
        self.assertIsNone(db.get_job(self.conn, c)["archived_at"])

    def test_cancelled_and_failed_auto_archive_after_24_hours(self):
        old = db.add_job(self.conn, "alex-rivera", "Old", "R", JD_TEXT)
        db.cancel(self.conn, old)
        new = db.add_job(self.conn, "alex-rivera", "New", "R", JD_TEXT)
        db.set_status(self.conn, new, "failed")
        ready_old = db.add_job(self.conn, "alex-rivera", "Done", "R", JD_TEXT)
        db.set_status(self.conn, ready_old, "ready")
        for jid in (old, ready_old):
            self.conn.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (_ago(25), jid))
        self.assertEqual(db.auto_archive(self.conn), [old])
        self.assertEqual(db.auto_archive(self.conn), [])


class ReasonsTests(unittest.TestCase):
    def test_plain_words_name_the_company_and_drop_machine_hints(self):
        out = reasons.plain({"checks": {"failures": [
            {"check": 4, "message": "bullet 5 of role 3 (Northwind Analytics, India) is a widow: its last line fills 16% of the "
                                    "width (minimum 20%); add or remove a few words"},
            {"check": 1, "message": "the PDF has 2 pages; it must be exactly 1 (shorten the longest bullets)"},
            {"check": 5, "message": "not in the base resumes or known skills: 'NoSQL'"}]}})
        self.assertEqual(out[0], {"area": "Line fit", "text": "Bullet 5 at Northwind Analytics is a widow: its last line is only 16% "
                                                              "wide (looks unfinished)"})
        self.assertEqual(out[1]["text"], "The resume runs onto 2 pages; it must fit on 1")
        self.assertEqual(out[2], {"area": "Honesty", "text": "Skills you have not listed anywhere: 'NoSQL'"})
        self.assertEqual(reasons.plain({}, "Claude did not answer"), [{"area": "Problem",
                                                                       "text": "Claude did not answer"}])


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.app = server.App()
        self.httpd = server.make_server(self.app, 0, "127.0.0.1")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.token = config.TOKEN_PATH.read_text().strip()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.__exit__(None, None, None)

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_review_actions_and_state(self):
        conn = self.app.conn
        a, b = _needs_you(conn), _needs_you(conn)
        status, data = self.call("/api/state")
        cards = {j["id"]: j for j in data["jobs"]}
        self.assertEqual(cards[a]["reasons"][0], {"area": "Writing",
                                                  "text": "Bullet 5 at Lumen Freight has 26 words; the rule is 15 to 25"})
        self.assertEqual(self.call(f"/api/jobs/{a}/accept", {})[1]["status"], "ready")
        self.assertEqual(self.call(f"/api/jobs/{b}/fix", {"note": ""})[0], 400)
        self.assertEqual(self.call(f"/api/jobs/{b}/fix", {"note": "keep bullet 2 as it was"})[1]["status"], "queued")
        self.assertEqual(db.get_job(conn, b)["fix_note"], "keep bullet 2 as it was")
        self.assertEqual(self.call(f"/api/jobs/{a}/archive", {})[1]["archived"], a)
        self.assertNotIn(a, {j["id"] for j in self.call("/api/state")[1]["jobs"]})
        self.assertIn(a, {j["id"] for j in self.call("/api/jobs?profile=alex-rivera")[1]["jobs"]})
        self.assertEqual(self.call("/api/archive", {"what": "nonsense"})[0], 400)
        c = db.add_job(conn, "alex-rivera", "ZZ_TEST Co", "R", JD_TEXT)
        db.cancel(conn, c)
        self.assertEqual(self.call("/api/archive", {"what": "cancelled_failed"})[1]["archived"], [c])
        state = self.call("/api/state")[1]
        self.assertEqual([j["id"] for j in state["archived"]][:1], [c])
        self.assertEqual(self.call("/api/unarchive", {"ids": "x"})[0], 400)
        self.assertEqual(self.call("/api/unarchive", {"ids": [c, a]})[1]["restored"], [c, a])
        self.assertEqual(self.call("/api/state")[1]["archived"], [])


@unittest.skipUnless(TOOLS, "poppler tools missing")
class FixWithNotePipelineTests(unittest.TestCase):
    """The note round changes only what the note names; later repairs may touch only that; the rest stays frozen."""

    def setUp(self):
        self.env = TempEnv({"max_attempts": 1, "max_repairs": 1}).__enter__()
        self.conn = db.connect()
        self.prof, self.base = example_profile()
        self.content = docxio.structure(docxio.read(self.base.docx)).as_content()
        self.content.update({"change_notes": [], "match": "m", "review": [], "gaps": [], "added_from_jd": []})
        self.patch = mock.patch.object(render, "to_pdf", side_effect=lambda *a, **k: (Path(self.base.pdf).read_bytes(),
                                                                                       "test"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _spoil(self, *bullets) -> dict:
        bad = json.loads(json.dumps(self.content))
        for k in bullets:
            words = bad["jobs"][0]["bullets"][k - 1].split()
            bad["jobs"][0]["bullets"][k - 1] = " ".join(["Spearheaded"] + words[1:])
        return bad

    def _needs_you_run(self, bad) -> dict:
        jid = db.add_job(self.conn, "alex-rivera", "Acme", "Machine Learning Engineer", JD_TEXT, base_override="ml_engineer")
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        runner = mock.Mock(side_effect=[_ok_result(bad), _ok_result(NO_FIX)])
        self.assertEqual(pipeline.process(self.conn, db.get_job(self.conn, jid), config.settings(), None, runner),
                         "needs_you")
        return db.get_job(self.conn, jid)

    def _fix(self, job, note, output) -> tuple:
        db.fix_with_note(self.conn, job["id"], note)
        db.claim(self.conn, job["id"], str(config.RUNS / str(job["id"])))
        runner = mock.Mock(return_value=_ok_result(output))
        status = pipeline.process(self.conn, db.get_job(self.conn, job["id"]), config.settings(), None, runner)
        return status, runner, db.get_job(self.conn, job["id"])

    def test_note_fixes_the_named_bullet_and_the_job_is_ready(self):
        job = self._needs_you_run(self._spoil(1))
        original = self.content["jobs"][0]["bullets"][0]
        status, runner, row = self._fix(job, "bullet 1 of role 1: say it without 'Spearheaded'",
                                        {"bullets": [{"job": 1, "index": 1, "text": original}], "skills": [],
                                         "summary": None, "notes": ["reworded"]})
        self.assertEqual(status, "ready")
        self.assertEqual(runner.call_count, 1)
        prompt = runner.call_args[0][0]
        self.assertIn("THE USER'S NOTE", prompt)
        self.assertIn("bullet 1 of role 1: say it without 'Spearheaded'", prompt)
        self.assertIn("Change ONLY what the note names", prompt)
        res = db.result_of(row)
        self.assertEqual(res["fix"]["changes"], ["Lumen Freight bullet 1"])
        self.assertTrue(res["fix"]["applied"])
        self.assertEqual(res["attempts"][-1]["kind"], "fix")
        self.assertIsNone(row["fix_note"])

    def test_one_call_then_the_problems_are_listed_for_the_user(self):
        job = self._needs_you_run(self._spoil(1, 2))
        original = self.content["jobs"][0]["bullets"][0]
        status, runner, row = self._fix(job, "fix bullet 1 of role 1 only",
                                        {"bullets": [{"job": 1, "index": 1, "text": original}], "skills": [],
                                         "summary": None, "notes": []})
        self.assertEqual(status, "needs_you")
        self.assertEqual(runner.call_count, 1)                 # ONE call, no repair round after the note
        self.assertIn("spearheaded", row["error"].lower())     # bullet 2 is listed as a problem, left as it is
        last = json.loads(Path(db.result_of(row)["attempts"][-1]["files"]["content"]).read_text())
        self.assertEqual(last["jobs"][0]["bullets"][0], original)
        self.assertTrue(last["jobs"][0]["bullets"][1].startswith("Spearheaded"))


if __name__ == "__main__":
    unittest.main()
