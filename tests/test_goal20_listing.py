"""A tracker reads every one of a person's resumes, not the newest 200 (at a few dozen a day an older approved
resume fell out of view in days). `?lean=1` lists all of them with only the small result fields a tracker uses,
so the answer stays small as the list grows."""
import json
import unittest

from resume_tailor import db
from tests import test_goal5_edits as g5

LEAN_KEYS = {"base", "approved", "dropped"}


class LeanListingTests(unittest.TestCase):
    setUp, tearDown, call = g5.ApiTests.setUp, g5.ApiTests.tearDown, g5.ApiTests.call

    def test_lean_lists_every_job_of_the_person_with_small_results(self):
        big = {"base": {"id": "ml"}, "approved": {"name": "x.pdf"}, "attempts": [{"n": 1}] * 50, "diff": "d" * 5000}
        for i in range(205):
            jid = db.add_job(self.conn, "alex-rivera", f"Co{i}", "R", "JD " * 30)
            db.update(self.conn, jid, result=json.dumps(big))
        db.add_job(self.conn, "sam-lee", "Other", "R", "JD " * 30)
        _, full = self.call("/api/jobs?profile=alex-rivera")
        self.assertEqual(len(full["jobs"]), 200)                       # unchanged without lean
        _, lean = self.call("/api/jobs?profile=alex-rivera&lean=1")
        self.assertEqual(len(lean["jobs"]), 205)
        self.assertTrue(all(j["profile"] == "alex-rivera" for j in lean["jobs"]))
        self.assertEqual(set(lean["jobs"][0]["result"]), {"base", "approved"} & LEAN_KEYS)
        self.assertIn("files_version", lean["jobs"][0])
        self.assertNotIn("jd_text", lean["jobs"][0])


if __name__ == "__main__":
    unittest.main()
