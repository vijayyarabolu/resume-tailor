"""a ready resume made from an old base, under the old title rule, or whose stored PDF fails the free checks
is reported as outdated with the reason; times() says when it was made and last updated."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from resume_tailor import db, docxio, outdated
from tests.helpers import JD_TEXT, TempEnv, example_profile


class OutdatedTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()
        self.prof, self.base = example_profile()
        outdated._file_problems.cache_clear()

    def tearDown(self):
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _ready(self, base_pdf=None, docx=None, sha=None, attempts=(), accepted=None) -> dict:
        jid = db.add_job(self.conn, "alex-rivera", "Acme", "Machine Learning Engineer", JD_TEXT)
        base = {"id": "ml_engineer", "label": "ML Engineer", **({"sha": sha} if sha else {})}
        result = {"base": base, "files": {"pdf": str(self.base.pdf), "docx": str(docx or self.base.docx),
                                          "base_pdf": str(base_pdf or self.base.pdf)}, "attempts": list(attempts)}
        if accepted:
            result["accepted"] = {"at": accepted, "despite": ["x"]}
        db.update(self.conn, jid, result=json.dumps(result))
        db.set_status(self.conn, jid, "ready")
        return db.get_job(self.conn, jid)

    def test_a_resume_from_the_current_base_is_current(self):
        self.assertEqual(outdated.outdated(self._ready(), self.prof), [])
        self.assertEqual(outdated.outdated(self._ready(sha=self.base.sha), self.prof), [])

    def test_an_archived_base_file_or_another_sha_is_the_old_resume(self):
        moved = self._ready(base_pdf="/x/resumes/archive/Alex_Rivera_ML_Engineer.pdf")
        reasons = outdated.outdated(moved, self.prof)
        self.assertEqual(reasons[0], outdated.OLD_BASE)
        self.assertEqual(outdated.badge(reasons), "Outdated: made from your old resume")
        self.assertIn(outdated.OLD_BASE, outdated.outdated(self._ready(sha="0" * 64), self.prof))

    def test_the_jds_title_under_the_name_is_the_old_title_rule(self):
        content = copy.deepcopy(self.base.structure.as_content())
        content["headline"] = "Applied AI Research Scientist"
        tmp = Path(tempfile.mkdtemp()) / "resume.docx"
        tmp.write_bytes(docxio.fill(docxio.read(self.base.docx), content))
        reasons = outdated.outdated(self._ready(docx=tmp), self.prof)
        self.assertEqual(reasons, ["old title rule: headline 'Applied AI Research Scientist' (now 'Machine Learning "
                                   "Engineer')"])
        self.assertEqual(outdated.badge(reasons), "Outdated: old title rule")

    def test_only_ready_jobs_are_judged_and_date_messages_are_short(self):
        job = {**self._ready(base_pdf="/old.pdf"), "status": "needs_you"}
        self.assertEqual(outdated.outdated(job, self.prof), [])
        self.assertEqual(outdated._short_date("the date 'May 2025' of Education wraps onto the next line in the PDF"),
                         "date wraps: May 2025 (Education)")
        self.assertEqual(outdated.badge(["date wraps: May 2025 (Education)"]),
                         "Outdated: date wraps: May 2025 (Education)")

    def test_times_made_by_the_last_run_updated_by_a_reimport_or_looks_good(self):
        runs = [{"kind": "full", "finished": "2026-10-07T10:00:00Z"}, {"kind": "repair", "finished": "2026-10-07T10:02:00Z"},
                {"kind": "reimport", "finished": "2026-10-07T12:00:00Z"}]
        t = outdated.times(self._ready(attempts=runs))
        self.assertEqual((t["made"], t["updated"]), ("2026-10-07T10:02:00Z", "2026-10-07T12:00:00Z"))
        t = outdated.times(self._ready(attempts=runs[:1], accepted="2026-10-07T11:00:00Z"))
        self.assertEqual((t["made"], t["updated"]), ("2026-10-07T10:00:00Z", "2026-10-07T11:00:00Z"))
        t = outdated.times(self._ready())
        self.assertIsNone(t["updated"])
        self.assertTrue(t["made"])


if __name__ == "__main__":
    unittest.main()
