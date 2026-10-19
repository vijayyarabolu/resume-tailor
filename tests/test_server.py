"""The HTTP API against a temp DB: token gate, allowlist, job creation/validation, actions, files, SSE."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from resume_tailor import config, db, profiles, server
from tests.helpers import JD_TEXT, TempEnv, add_second_base


class AllowlistTests(unittest.TestCase):
    def test_this_machine_only_by_default(self):
        self.assertTrue(server.client_allowed("127.0.0.1", False))
        self.assertTrue(server.client_allowed("::1", False))
        self.assertFalse(server.client_allowed("100.101.102.103", False))
        self.assertFalse(server.client_allowed("fd7a:115c:a1e0::1234", False))
        self.assertTrue(server.client_allowed("::ffff:127.0.0.1", False))
        self.assertFalse(server.client_allowed("192.168.1.20", False))
        self.assertFalse(server.client_allowed("8.8.8.8", False))
        self.assertFalse(server.client_allowed("garbage", False))

    def test_lan_only_when_enabled(self):
        self.assertTrue(server.client_allowed("192.168.1.20", True))
        self.assertTrue(server.client_allowed("10.0.0.5", True))
        self.assertFalse(server.client_allowed("8.8.8.8", True))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv(people=("alex-rivera", "sam-lee")).__enter__()
        cls.app = server.App()
        cls.httpd = server.make_server(cls.app, 0, "127.0.0.1")
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = config.TOKEN_PATH.read_text().strip()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.__exit__(None, None, None)

    def call(self, path, body=None, token="ok", method=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token == "ok":
            req.add_header("Authorization", f"Bearer {self.token}")
        elif token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}"), err.headers

    def test_token_file_is_private(self):
        self.assertEqual(config.TOKEN_PATH.stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(len(self.token), 24)

    def test_gate_rejects_missing_and_wrong_token(self):
        self.assertEqual(self.call("/api/state", token=None)[0], 401)
        self.assertEqual(self.call("/api/state", token="wrong")[0], 401)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_index_sets_cookie_once_from_query_token(self):
        url = f"http://127.0.0.1:{self.port}/?token={self.token}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            cookie = resp.headers.get("Set-Cookie")
            html = resp.read().decode()
        self.assertIn("rg_token=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("<title>Resume Tailor</title>", html)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/state")
        req.add_header("Cookie", cookie.split(";")[0])
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_static_assets_served_without_token_but_nothing_else(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/app.js", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("javascript", resp.headers["Content-Type"])
        self.assertEqual(self.call("/static/../config.py", token=None)[0], 404)

    def test_add_jobs_validates_and_creates(self):
        status, data, _ = self.call("/api/jobs", {"profile": "nobody", "jd_text": JD_TEXT})
        self.assertEqual((status, data["ok"]), (400, False))
        self.assertIn("unknown profile", data["error"])
        status, data, _ = self.call("/api/jobs", {"profile": "alex-rivera", "jd_text": "too short"})
        self.assertEqual(status, 400)
        status, data, _ = self.call("/api/jobs", {"profile": "alex-rivera", "jd_text": JD_TEXT, "base_override": "nope"})
        self.assertEqual(status, 400)
        status, data, _ = self.call("/api/jobs", {"jobs": [
            {"profile": "alex-rivera", "company": "Acme", "role": "ML Engineer", "jd_text": JD_TEXT, "tracker_job_id": "ex:1"},
            {"profile": "sam-lee", "jd_text": JD_TEXT}]})
        self.assertEqual((status, data["ok"], len(data["ids"])), (201, True, 2))
        job = db.get_job(self.app.conn, data["ids"][1])
        self.assertEqual(job["profile"], "sam-lee")
        self.assertEqual(job["company"], "Acme")                 # guessed from "About Acme"
        self.assertIn("Machine Learning Engineer", job["role"])  # guessed from the first line
        status, data, _ = self.call(f"/api/jobs/{data['ids'][0]}")
        self.assertEqual(data["job"]["tracker_job_id"], "ex:1")
        self.assertNotIn("jd_text", data["job"])
        self.assertEqual(data["jd_text"], JD_TEXT.strip())

    def test_state_lists_profiles_jobs_settings(self):
        self.call("/api/jobs", {"profile": "alex-rivera", "company": "Beta", "role": "AI Engineer", "jd_text": JD_TEXT})
        status, data, _ = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual({p["id"] for p in data["profiles"]}, {"alex-rivera", "sam-lee"})
        self.assertTrue(any(j["company"] == "Beta" for j in data["jobs"]))
        self.assertIn("model", data["settings"])
        self.assertFalse(data["worker"])

    def test_cancel_retry_and_file_404(self):
        _, data, _ = self.call("/api/jobs", {"profile": "alex-rivera", "company": "Gamma", "role": "DS", "jd_text": JD_TEXT})
        jid = data["ids"][0]
        status, data, _ = self.call(f"/api/jobs/{jid}/cancel", {})
        self.assertEqual(data["status"], "cancelled")
        status, data, _ = self.call(f"/api/jobs/{jid}/retry", {"note": "shorter bullets"})
        self.assertEqual(data["status"], "queued")
        self.assertEqual(db.get_job(self.app.conn, jid)["note"], "shorter bullets")
        self.assertEqual(self.call(f"/api/jobs/{jid}/file/pdf")[0], 404)
        self.assertEqual(self.call(f"/api/jobs/{jid}/approve", {})[0], 400)
        self.assertEqual(self.call("/api/jobs/999999/cancel", {})[0], 404)

    def test_top_action_moves_a_queued_job_first(self):
        ids = [self.call("/api/jobs", {"profile": "sam-lee", "company": c, "role": "DS", "jd_text": JD_TEXT})[1]["ids"][0]
               for c in ("Top1", "Top2")]
        status, data, _ = self.call(f"/api/jobs/{ids[1]}/top", {})
        self.assertEqual((status, data["status"]), (200, "queued"))
        self.assertEqual(db.next_queued(self.app.conn, "sam-lee")["id"], ids[1])
        _, listed, _ = self.call("/api/jobs?profile=sam-lee")
        prio = {j["id"]: j["priority"] for j in listed["jobs"]}
        self.assertGreater(prio[ids[1]], prio[ids[0]])
        for jid in ids:
            self.call(f"/api/jobs/{jid}/cancel", {})

    def test_settings_update_persists(self):
        status, data, _ = self.call("/api/settings", {"model": "sonnet"})
        self.assertEqual(data["settings"]["model"], "sonnet")
        self.assertEqual(json.loads(config.SETTINGS_PATH.read_text())["model"], "sonnet")
        self.call("/api/settings", {"model": "default"})
        self.assertEqual(json.loads(config.SETTINGS_PATH.read_text())["model"], config.DEFAULTS["model"])

    def test_settings_are_validated(self):
        for body in ({"model": "--add-dir=/"}, {"effort": "turbo"}, {"pdf_engine": "photoshop"}, {"claude_backend": "magic"}, {"tracker_url": "javascript:alert(1)"},
                     {"concurrency": "abc"}, {"max_attempts": 99}):
            status, data, _ = self.call("/api/settings", body)
            self.assertEqual(status, 400, body)
        self.assertEqual(server.validate_settings({"model": "claude-opus-5-5", "concurrency": "2"}),
                         {"model": "claude-opus-5-5", "concurrency": 2})

    def test_query_token_only_on_index_and_never_logged(self):
        status, _, _ = self.call(f"/api/state?token={self.token}", token=None)
        self.assertEqual(status, 401)
        with self.assertLogs("resume_tailor.server", level="INFO") as logs:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/?token={self.token}", timeout=5):
                pass
        self.assertTrue(any("token=<redacted>" in line for line in logs.output))
        self.assertFalse(any(self.token in line for line in logs.output))

    def test_jd_guess_endpoint(self):
        status, data, _ = self.call("/api/jd/guess", {"text": JD_TEXT})
        self.assertEqual((data["company"], data["role"]), ("Acme", "Senior Machine Learning Engineer"))

    def test_jd_fetch_refuses_linkedin(self):
        status, data, _ = self.call("/api/jd/fetch", {"url": "https://www.linkedin.com/jobs/view/123"})
        self.assertFalse(data["ok"])
        self.assertIn("paste", data["error"].lower())

    def test_events_stream_sends_rows(self):
        self.call("/api/jobs", {"profile": "alex-rivera", "company": "Delta", "role": "DE", "jd_text": JD_TEXT})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/events?since=0")
        req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIn("text/event-stream", resp.headers["Content-Type"])
            first = resp.readline().decode()
            second = resp.readline().decode()
        self.assertTrue(first.startswith("id: "))
        self.assertTrue(second.startswith("data: "))
        self.assertEqual(json.loads(second[6:])["kind"], "status")

    def test_file_name_pattern(self):
        prof = profiles.load("alex-rivera")
        name = server.file_name_for(prof, {"role": "ML Engineer", "company": "Acme / Co", "result": None})
        self.assertEqual(name, "Alex_Rivera_ML_Engineer_Resume(Acme Co).pdf")

    def test_names_follow_the_role_not_the_base(self):
        """A "Senior Machine Learning Engineer, Growth" role built from the Data Scientist base."""
        add_second_base()
        prof = profiles.load("alex-rivera")
        job = {"base_id": "data_scientist", "role": "Senior Machine Learning Engineer, Growth", "company": "Acme",
               "result": json.dumps({"headline": "Machine Learning Engineer"})}
        self.assertEqual(server.download_name(prof, job, "pdf"), "Alex_Rivera_ML_Engineer.pdf")
        self.assertEqual(server.file_name_for(prof, job), "Alex_Rivera_ML_Engineer(Acme).pdf")
        job_ds = {**job, "role": "Senior Data Scientist", "result": None}       # no result yet: the role decides
        self.assertEqual(server.download_name(prof, job_ds, "docx"), "Alex_Rivera_Data_Scientist.docx")
        aiml = {**job, "result": json.dumps({"headline": "AI/ML Engineer"})}   # no such base: the person's style
        self.assertEqual(server.download_name(prof, aiml, "pdf"), "Alex_Rivera_AI_ML_Engineer.pdf")


if __name__ == "__main__":
    unittest.main()
