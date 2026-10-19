"""a Greenhouse link (job-boards.greenhouse.io/coreweave/jobs/4720302006) redirected to CoreWeave's own
careers page, which loads the job with JavaScript: resume_tailor read the site menu as the JD (5.8 KB of navigation) and
"Explore Our Open Positions | CoreWeave" gave the company "Explore Our Open Positions". Greenhouse, Lever and Ashby
links are now read through their public job APIs; the title guess skips careers-page phrases."""
import json
import unittest
from unittest import mock

from resume_tailor import jd

BODY = "<p>Build agents with Python and LLMs.</p>" + "<p>We ship APIs every week for customers.</p>" * 6


def _answer(url_seen: list, payload: dict):
    def get(url, hops=0):
        url_seen.append(url)
        return json.dumps(payload).encode(), "application/json"
    return get


class AtsApiTests(unittest.TestCase):
    def test_greenhouse_links_read_the_job_api(self):
        payload = {"title": "Software and AI Engineer", "company_name": "CoreWeave",
                   "content": BODY.replace("<", "&lt;").replace(">", "&gt;")}
        for url in ("https://job-boards.greenhouse.io/coreweave/jobs/4720302006",
                    "https://boards.greenhouse.io/coreweave/jobs/4720302006?t=1",
                    "https://coreweave.com/careers/job?4720302006&board=coreweave&gh_jid=4720302006"):
            seen = []
            with mock.patch.object(jd, "_get", _answer(seen, payload)):
                got = jd.fetch(url)
            api = "https://boards-api.greenhouse.io/v1/boards/coreweave/jobs/4720302006?content=true"
            self.assertEqual(seen, [api], url)
            self.assertEqual((got["company"], got["role"], got["source"]),
                             ("CoreWeave", "Software and AI Engineer", "greenhouse"))
            self.assertIn("Build agents with Python", got["text"])
            self.assertNotIn("<p>", got["text"])

    def test_lever_links_read_the_posting_api(self):
        payload = {"text": "ML Engineer", "descriptionPlain": "Train models. " * 20,
                   "lists": [{"text": "What you will do", "content": "<li>Ship models</li>"}], "additionalPlain": ""}
        seen = []
        with mock.patch.object(jd, "_get", _answer(seen, payload)):
            got = jd.fetch("https://jobs.lever.co/acme-ai/0f3c5b2a-1111-2222-3333-444455556666/apply")
        self.assertEqual(seen, ["https://api.lever.co/v0/postings/acme-ai/0f3c5b2a-1111-2222-3333-444455556666"])
        self.assertEqual((got["company"], got["role"], got["source"]), ("Acme Ai", "ML Engineer", "lever"))
        self.assertIn("What you will do", got["text"])
        self.assertIn("Ship models", got["text"])

    def test_ashby_links_read_the_job_board_api(self):
        jid = "7d1e3c4b-aaaa-bbbb-cccc-ddddeeeeffff"
        payload = {"jobs": [{"id": "other", "title": "Recruiter", "descriptionPlain": "x" * 300},
                            {"id": jid, "title": "Applied AI Engineer",
                             "descriptionPlain": "Agents in production. " * 20}]}
        seen = []
        with mock.patch.object(jd, "_get", _answer(seen, payload)):
            got = jd.fetch(f"https://jobs.ashbyhq.com/beta-labs/{jid}")
        self.assertEqual(seen, ["https://api.ashbyhq.com/posting-api/job-board/beta-labs"])
        self.assertEqual((got["company"], got["role"], got["source"]), ("Beta Labs", "Applied AI Engineer", "ashby"))

    def test_an_api_miss_says_paste_the_text(self):
        def gone(url, hops=0):
            raise jd.JdError("the page answered 404: paste the job text instead")
        with mock.patch.object(jd, "_get", gone), self.assertRaises(jd.JdError):
            jd.fetch("https://job-boards.greenhouse.io/coreweave/jobs/1")


class TitleGuessTests(unittest.TestCase):
    def test_careers_page_phrases_are_not_a_company(self):
        self.assertEqual(jd.guess("", "Explore Our Open Positions | CoreWeave"), ("CoreWeave", ""))
        self.assertEqual(jd.guess("", "Current Openings - Acme"), ("Acme", ""))


if __name__ == "__main__":
    unittest.main()
