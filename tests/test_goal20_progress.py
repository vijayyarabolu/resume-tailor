"""(user 22:21): a running Re-import says what it is doing ("reading your Doc" → "building the PDF" →
"checking") for both apps' busy lines, through the job view; the step times are kept with the result so a slow step
can be found later."""
import threading
import unittest
from unittest import mock

from resume_tailor import config, db, docedit, pipeline, progress, render
from tests import test_goal5_edits as g5


class StepsTests(unittest.TestCase):
    setUp, tearDown, _job, _doc = g5.ReimportTests.setUp, g5.ReimportTests.tearDown, g5.ReimportTests._job, g5.ReimportTests._doc

    def test_the_steps_are_told_while_it_runs_and_kept_with_the_result(self):
        seen = []
        real_pdf, real_checks = render.to_pdf, pipeline.check_files

        def pdf(*a, **k):
            seen.append(progress.get(self.jid)["step"])
            return real_pdf(*a, **k)

        def checks(*a, **k):
            seen.append(progress.get(self.jid)["step"])
            return real_checks(*a, **k)
        with mock.patch.object(render, "to_pdf", side_effect=pdf), mock.patch.object(pipeline, "check_files", side_effect=checks):
            docedit.reimport(self.conn, self._job(), self._doc(), config.settings(), None)
        self.assertEqual(seen, ["building the PDF", "checking"])
        steps = db.result_of(self._job())["reimport"]["steps"]
        self.assertEqual([s for s, _ in steps], ["building the PDF", "checking"])
        self.assertTrue(all(isinstance(sec, float) and sec >= 0 for _, sec in steps), steps)
        self.assertIsNone(progress.get(self.jid))

    def test_a_crash_clears_the_step(self):
        with mock.patch.object(render, "to_pdf", side_effect=RuntimeError("engine down")):
            with self.assertRaises(RuntimeError):
                docedit.reimport(self.conn, self._job(), self._doc(), config.settings(), None)
        self.assertIsNone(progress.get(self.jid))


class ApiStepTests(unittest.TestCase):
    setUp, tearDown, call, _made = g5.ApiTests.setUp, g5.ApiTests.tearDown, g5.ApiTests.call, g5.ApiTests._made

    def test_the_job_view_tells_the_step_while_the_doc_is_read(self):
        jid = self._made()
        db.update(self.conn, jid, gdoc_id="D1", gdoc_link="https://docs.example/D1")
        go, inside = threading.Event(), threading.Event()

        class Doc:
            def get(self, *a, **k):
                return {"version": "1"}

            def export(self, *a, **k):
                inside.set()
                go.wait(5)
                return b"docx"
        with mock.patch.object(self.app, "with_google", side_effect=lambda pid, fn: fn(Doc())), \
                mock.patch.object(docedit, "reimport", return_value="ready"):
            t = threading.Thread(target=self.call, args=(f"/api/jobs/{jid}/reimport", {}))
            t.start()
            self.assertTrue(inside.wait(5))
            _, view = self.call(f"/api/jobs/{jid}")
            go.set()
            t.join(5)
        self.assertEqual((view["job"].get("step") or {}).get("step"), "reading your Doc", view["job"].get("step"))
        _, after = self.call(f"/api/jobs/{jid}")
        self.assertIsNone(after["job"].get("step"))


if __name__ == "__main__":
    unittest.main()
