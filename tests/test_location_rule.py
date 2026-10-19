"""The contact line's location follows the job. A tracker sends the resolved "City, ST" for a hybrid or on-site
job (none for a remote one), or the app reads it from the JD; it is written in place of "Portland, OR", every other
run of the contact line stays, and the location check accepts exactly that city."""
import tempfile
import types
import unittest
from unittest import mock
import zipfile
from io import BytesIO
from pathlib import Path

from resume_tailor import checks, db, docxio, places, profiles, rules, server
from tests.helpers import TempEnv

HOME = "Portland, OR"
JD_TEXT = "About Acme\nAcme builds AI tools. " * 10


def _links(docx_bytes: bytes) -> int:
    with zipfile.ZipFile(BytesIO(docx_bytes)) as z:
        return z.read("word/document.xml").decode().count("<w:hyperlink")


def _structure(docx_bytes: bytes) -> docxio.Structure:
    path = Path(tempfile.mkdtemp()) / "t.docx"
    path.write_bytes(docx_bytes)
    return docxio.structure(docxio.read(path))


class LocationRule(unittest.TestCase):
    def setUp(self):
        self.prof = profiles.load("alex-rivera")
        self.base = self.prof.base("ml_engineer")

    def _rules(self, location):
        return rules.for_job(rules.for_profile("alex-rivera", self.prof.rules), "AI Engineer", self.base.structure.headline, location)

    def test_onsite_job_city_goes_on_the_contact_line_and_links_stay(self):
        content = {**self.base.structure.as_content(), "contact_location": (HOME, "Wilmington, MA")}
        filled = docxio.fill(docxio.read(self.base.docx), content)
        got = _structure(filled)
        self.assertEqual(got.contact, self.base.structure.contact.replace(HOME, "Wilmington, MA"))
        self.assertEqual(_links(filled), _links(self.base.docx.read_bytes()), "LinkedIn / GitHub links kept")
        place = rules.contact_location(self._rules("Wilmington, MA"))
        self.assertEqual(place, (HOME, "Wilmington, MA"))
        self.assertEqual(docxio.layout_diffs(self.base.structure, got, True, place), [])
        self.assertTrue(any("contact line changed" in p for p in docxio.layout_diffs(self.base.structure, got)),
                        "without the job's city the base contact line is required")

    def test_remote_or_unknown_keeps_the_persons_city(self):
        for location in (None, "", HOME):
            jr = self._rules(location)
            self.assertEqual(jr["location"], HOME)
            self.assertIsNone(rules.contact_location(jr))

    def test_location_check_accepts_exactly_the_jobs_city(self):
        tstruct = docxio.structure(docxio.read(self.base.docx))           # still says Portland, OR
        inp = dict(pid="alex-rivera", profile_rules=self.prof.rules, base_id=self.base.id, role="AI Engineer",
                   base_structure=self.base.structure, all_base_texts=[b.text for b in self.prof.bases],
                   base_measure=types.SimpleNamespace(bullets_end_with_period=False))
        onsite = [f.name for f in checks.check_writing(types.SimpleNamespace(**inp, location="Wilmington, MA"), tstruct)]
        self.assertIn("location", onsite)
        remote = [f.name for f in checks.check_writing(types.SimpleNamespace(**inp, location=""), tstruct)]
        self.assertNotIn("location", remote)


class ApiTakesTheLocation(unittest.TestCase):
    def test_validate_and_store(self):
        with TempEnv():
            conn = db.connect()
            known = set(profiles.ids())
            ok = server.Handler._validate_job(None, {"profile": "alex-rivera", "company": "Acme",
                                                     "role": "Embedded AI Tools Engineer", "jd_text": JD_TEXT,
                                                     "workplace": "onsite", "location": "Wilmington, MA"}, known)
            job = db.get_job(conn, db.add_job(conn, *ok))
            self.assertEqual((job["workplace"], job["location"]), ("onsite", "Wilmington, MA"))
            for bad in ({"workplace": "office"},):       # a location is never refused (test_goal22_place)
                with self.assertRaises(ValueError):
                    server.Handler._validate_job(None, {"profile": "alex-rivera", "jd_text": JD_TEXT, "company": "A",
                                                        "role": "R", **bad}, known)
            conn.close()


class ManualJdPlace(unittest.TestCase):
    """A pasted JD (no tracker job) gets its workplace and city from the JD text; a job a tracker sent keeps what
    the tracker resolved."""
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_a_manual_jd_takes_the_jds_place_once(self):
        from resume_tailor import pipeline
        jd = "Data Engineer\nLocation: Temple, TX (Hybrid)\n" + "We build pipelines. " * 20
        job_id = db.add_job(self.conn, "alex-rivera", "Acme", "Data Engineer", jd)
        with mock.patch.object(places, "place", wraps=places.place) as place:
            job = pipeline.place_from_jd(self.conn, db.get_job(self.conn, job_id))
            again = pipeline.place_from_jd(self.conn, job)
        place.assert_called_once_with(jd)
        self.assertEqual((job["workplace"], job["location"]), ("hybrid", "Temple, TX"))
        self.assertEqual(db.get_job(self.conn, job_id)["location"], "Temple, TX")
        self.assertIs(again, job, "resolved once")

    def test_a_trackers_job_keeps_its_place_and_a_jd_without_one_keeps_the_home_city(self):
        from resume_tailor import pipeline
        sent = db.get_job(self.conn, db.add_job(self.conn, "alex-rivera", "Acme", "Data Engineer", "x" * 300,
                                                tracker_job_id="lever:acme:1", workplace="remote"))
        with mock.patch.object(places, "place") as place:
            self.assertIs(pipeline.place_from_jd(self.conn, sent), sent)
        place.assert_not_called()
        manual = db.get_job(self.conn, db.add_job(self.conn, "alex-rivera", "Acme", "Data Engineer", "x" * 300))
        self.assertIsNone(pipeline.place_from_jd(self.conn, manual)["location"])
        remote = db.get_job(self.conn, db.add_job(self.conn, "alex-rivera", "Acme", "Data Engineer",
                                                  "Fully remote. Location: Austin, TX\n" + "x " * 100))
        self.assertEqual((pipeline.place_from_jd(self.conn, remote)["workplace"],
                          pipeline.place_from_jd(self.conn, remote)["location"]), ("remote", None))


if __name__ == "__main__":
    unittest.main()
