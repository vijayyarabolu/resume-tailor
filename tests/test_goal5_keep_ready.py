"""A re-import stays in Ready, with a "problems" warning, when its only problems were already there before the
edit (rules added since the resume was made); it moves to needs you only when the person's edit caused a new
problem, and then says so. Both profiles."""
import json
import unittest
from unittest import mock

from resume_tailor import checks, config, db, docedit, docxio, pipeline, profiles, reasons, render
from tests.helpers import JD_TEXT, TempEnv
from tests.test_goal5_edits import HOME, TOOLS
from tests.test_pipeline import _ok_result

OLD_RULE = checks.Failure(7, "summary_start", 'the summary must open with "AI Engineer with 6 years"')
NEW_RULE = checks.Failure(7, "contact_place", "the contact line says Tempe, AZ; this posting is in Oregon")


def _writing_with_rules(real):
    """Today's rules: one the resume already broke before the edit, one only a Tempe edit breaks."""
    def check_writing(inp, tstruct):
        out = real(inp, tstruct) + [OLD_RULE]
        return out + [NEW_RULE] if "Tempe" in tstruct.contact else out
    return check_writing


class _KeepReady:
    PROFILE = ""

    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()
        self.prof = profiles.load(self.PROFILE)
        self.base = self.prof.bases[0]
        self.content = self.base.structure.as_content()
        self.content.update({"change_notes": ["c"], "match": "m", "review": ["r"], "gaps": [], "added_from_jd": []})
        self.render = mock.patch.object(render, "to_pdf", side_effect=lambda *a, **k: (self.base.pdf.read_bytes(), "test"))
        self.render.start()
        jid = db.add_job(self.conn, self.PROFILE, "Acme Health", "Machine Learning Engineer", JD_TEXT,
                         base_override=self.base.id)
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        pipeline.process(self.conn, db.get_job(self.conn, jid), config.settings(), None,
                         mock.Mock(return_value=_ok_result(self.content)))
        db.set_status(self.conn, jid, "ready", "made (and approved) before the new rules")
        self.jid = jid
        self.rules = mock.patch.object(checks, "check_writing", _writing_with_rules(checks.check_writing))
        self.rules.start()

    def tearDown(self):
        self.rules.stop()
        self.render.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _job(self):
        return db.get_job(self.conn, self.jid)

    def _doc(self, place: str) -> bytes:
        """The person's Doc after they changed the place on the contact line."""
        content = {**json.loads(json.dumps(self.content)), "contact_location": (HOME, place)}
        return docxio.fill(docxio.read(self.base.docx), content)

    def _reimport(self, place: str) -> tuple:
        status = docedit.reimport(self.conn, self._job(), self._doc(place), config.settings(), None)
        row = self._job()
        return status, row, db.result_of(row)

    def test_problems_that_were_there_before_the_edit_keep_it_in_ready(self):
        status, row, res = self._reimport("PDX, OR")
        self.assertEqual((status, row["status"]), ("ready", "ready"))
        self.assertIn("PDX, OR", docxio.structure(docxio.read(res["files"]["docx"])).contact)
        self.assertIn(OLD_RULE.message, res["reimport"]["old_problems"])
        self.assertNotIn(NEW_RULE.message, res["reimport"]["old_problems"])
        self.assertEqual(res["reimport"]["new_problems"], [])
        self.assertIsNone(res["reimport"]["moved"])
        self.assertIn(OLD_RULE.message, [f["message"] for f in res["checks"]["failures"]])   # the warning's list
        marked = [r for r in reasons.plain(res, row["error"]) if r.get("old")]
        self.assertTrue(marked and reasons.BEFORE_EDIT in marked[0]["text"])

    def test_a_new_problem_from_the_edit_moves_it_to_needs_you_and_says_where(self):
        status, row, res = self._reimport("Tempe, AZ")
        self.assertEqual((status, row["status"]), ("needs_you", "needs_you"))
        self.assertEqual(res["reimport"]["moved"], {"from": "ready", "to": "needs_you"})
        self.assertEqual(res["reimport"]["new_problems"], [NEW_RULE.message])
        self.assertIn(OLD_RULE.message, res["reimport"]["old_problems"])
        self.assertNotIn(NEW_RULE.message, res["reimport"]["old_problems"])
        self.assertTrue(row["error"].startswith(NEW_RULE.message))        # the edit's own problem first
        why = {r["text"].split(" (")[0]: r.get("old", False) for r in reasons.plain(res, row["error"])}
        self.assertFalse(why[reasons.plain_one(7, NEW_RULE.message)["text"]])
        self.assertTrue(why[reasons.plain_one(7, OLD_RULE.message)["text"].split(" (")[0]])

    def test_a_needs_you_resume_with_only_old_problems_stays_needs_you(self):
        db.set_status(self.conn, self.jid, "needs_you", error=OLD_RULE.message)
        status, row, res = self._reimport("PDX, OR")
        self.assertEqual(status, "needs_you")
        self.assertIsNone(res["reimport"]["moved"])
        self.assertEqual(res["reimport"]["new_problems"], [])

    def test_when_the_version_before_cannot_be_rechecked_every_problem_counts_as_new(self):
        real = pipeline.check_files
        calls = iter([OSError("gone")])
        def flaky(*a, **k):
            err = next(calls, None)
            if err:
                raise err
            return real(*a, **k)
        with mock.patch.object(pipeline, "check_files", side_effect=flaky):
            status, row, res = self._reimport("PDX, OR")
        self.assertEqual(status, "needs_you")
        self.assertEqual(res["reimport"]["moved"], {"from": "ready", "to": "needs_you"})
        self.assertIn(OLD_RULE.message, res["reimport"]["new_problems"])


@unittest.skipUnless(TOOLS, "needs the PDF tools")
class AlexKeepReadyTests(_KeepReady, unittest.TestCase):
    PROFILE = "alex-rivera"


@unittest.skipUnless(TOOLS, "needs the PDF tools")
class SamKeepReadyTests(_KeepReady, unittest.TestCase):
    PROFILE = "sam-lee"


class ProblemKeyTests(unittest.TestCase):
    def test_a_widow_measured_a_little_differently_is_the_same_problem(self):
        a = checks.Failure(4, "widow", "bullet 3 of role 3 is a widow: its last line fills 12% of the width")
        b = checks.Failure(4, "widow", "bullet 3 of role 3 is a widow: its last line fills 9.5 % of the width")
        c = checks.Failure(4, "widow", "bullet 2 of role 3 is a widow: its last line fills 12% of the width")
        self.assertEqual(docedit._problem_key(a), docedit._problem_key(b))
        self.assertNotEqual(docedit._problem_key(a), docedit._problem_key(c))
