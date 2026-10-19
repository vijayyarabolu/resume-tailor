import unittest
from unittest import mock

from resume_tailor import jd


class GuessTests(unittest.TestCase):
    def test_from_page_title(self):
        self.assertEqual(jd.guess("", "Senior ML Engineer - Acme"), ("Acme", "Senior ML Engineer"))
        self.assertEqual(jd.guess("", "Acme | AI Engineer"), ("Acme", "AI Engineer"))
        self.assertEqual(jd.guess("", "Data Scientist at Beta Labs"), ("Beta Labs", "Data Scientist"))

    def test_from_text(self):
        text = "Machine Learning Engineer\nRemote\nAbout Gamma Robotics\nWe build robots."
        self.assertEqual(jd.guess(text), ("Gamma Robotics", "Machine Learning Engineer"))

    def test_labelled_lines_from_a_trackers_copy_header(self):
        # a tracker's copy puts "Company:" and "Title:" lines on top of the description
        text = ("Company: Acme\nTitle: Data Engineer\nLocation: Reston, VA\nURL: https://x.example/1\n\n" + "-" * 40 +
                "\n\nAt Acme our modernization group is a consulting organization.")
        self.assertEqual(jd.guess(text), ("Acme", "Data Engineer"))
        self.assertEqual(jd.guess("Job Title: Senior ML Engineer\nAbout Delta Robotics\nWe build robots."),
                         ("Delta Robotics", "Senior ML Engineer"))

    def test_nothing_found(self):
        self.assertEqual(jd.guess("lorem ipsum dolor"), ("", ""))


class FetchTests(unittest.TestCase):
    def test_blocked_sites_are_refused(self):
        for url in ("https://www.linkedin.com/jobs/view/1", "https://linkedin.com/x", "https://indeed.com/viewjob"):
            self.assertIsNotNone(jd.blocked_site(url), url)
        self.assertIsNone(jd.blocked_site("https://boards.greenhouse.io/acme/jobs/1"))

    def test_fetch_rejects_bad_urls(self):
        for url in ("", "ftp://x", "not a url", "http://localhost/x", "http://127.0.0.1:8770/"):
            with self.assertRaises(jd.JdError, msg=url):
                jd.fetch(url)

    def test_private_and_local_addresses_are_refused(self):
        for url in ("http://127.0.0.1:8770/", "http://localhost/x", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.5/", "http://100.100.1.1/", "https://example.com:8443/"):
            with self.assertRaises(jd.JdError, msg=url):
                jd._public_url(url)

    def test_redirects_are_revalidated_hop_by_hop(self):
        import urllib.error
        hop = urllib.error.HTTPError("https://example.com/a", 302, "Found", {"Location": "http://127.0.0.1/secret"}, None)
        with mock.patch.object(jd, "robots_allows", return_value=True), \
                mock.patch.object(jd, "_public_url", side_effect=[None, jd.JdError("private")]), \
                mock.patch.object(jd._OPENER, "open", side_effect=hop):
            with self.assertRaises(jd.JdError) as ctx:
                jd.fetch("https://example.com/a")
        self.assertIn("private", str(ctx.exception))

    def test_fetch_honours_robots(self):
        with mock.patch.object(jd, "robots_allows", return_value=False), mock.patch.object(jd, "_public_url"):
            with self.assertRaises(jd.JdError) as ctx:
                jd.fetch("https://example.com/jobs/1")
        self.assertIn("robots", str(ctx.exception).lower())

    def test_html_to_text_and_json_ld(self):
        html = """<html><head><title>AI Engineer - Acme</title>
        <script type="application/ld+json">{"@type": "JobPosting", "title": "AI Engineer",
        "hiringOrganization": {"name": "Acme"}, "description": "<p>Build <b>agents</b> for customers who need document automation, retrieval and evaluation pipelines that run in production every day of the week and are monitored for drift, latency and cost by a small platform team.</p><ul><li>Python</li></ul>"}
        </script></head><body><nav>menu</nav><h1>AI Engineer</h1><p>Build agents.</p></body></html>"""
        text = jd.html_to_text("<p>One</p><p>Two &amp; three</p><script>x()</script>")
        self.assertIn("One", text)
        self.assertIn("Two & three", text)
        self.assertNotIn("x()", text)
        fake = mock.MagicMock()
        fake.read.return_value = html.encode()
        fake.headers = {"Content-Type": "text/html; charset=utf-8"}
        fake.__enter__.return_value = fake
        with mock.patch.object(jd, "robots_allows", return_value=True), \
                mock.patch.object(jd, "_public_url"), mock.patch.object(jd._OPENER, "open", return_value=fake):
            found = jd.fetch("https://example.com/jobs/1")
        self.assertEqual((found["company"], found["role"]), ("Acme", "AI Engineer"))
        self.assertIn("Python", found["text"])
        self.assertEqual(found["source"], "json-ld")


if __name__ == "__main__":
    unittest.main()
