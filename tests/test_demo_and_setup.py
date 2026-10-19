"""Demo mode (no key, no network) and the Settings page's API: status rows, saving a key, the Google client,
profile files and uploads, the demo profile. Plus one real end-to-end run through LibreOffice when it is
installed."""
import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from resume_tailor import config, db, demo_runner, docxio, pipeline, profiles, prompts, render, server
from tests.helpers import BASE_STEM, JD_TEXT, PID, TempEnv, example_profile

JOB = (config.EXAMPLES / "jobs" / "acme-senior-ml-engineer.txt").read_text()


class DemoRunnerTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _prompt(self, jd=JOB):
        prof, base = example_profile()
        measure = pipeline.measure_base(base)
        scores = pipeline._score(prof, {"jd_text": jd, "role": "Senior Machine Learning Engineer", "company": "Acme",
                                        "id": 0}, None)
        return prompts.build(prof, base, measure, jd, "Acme", "Senior Machine Learning Engineer", scores), base

    def test_it_keeps_every_bullet_and_adds_owned_keywords(self):
        prompt, base = self._prompt()
        res = demo_runner.run(prompt, prompts.SCHEMA)
        self.assertTrue(res.ok)
        out = res.output
        self.assertEqual([j["bullets"] for j in out["jobs"]], [j["bullets"] for j in base.structure.jobs])
        added = {i for s in out["skills"] for i in s["items"]} - {i for s in base.structure.as_content()["skills"]
                                                                  for i in s["items"]}
        self.assertTrue(added)
        self.assertTrue(added <= set(profiles.load(PID).known_skills), added)        # never a skill Alex lacks
        self.assertEqual(len(out["skills"]), len(base.structure.skills))
        docxio.fill(docxio.read(base.docx), out)                                    # fits the base layout

    def test_repair_and_note_rounds_change_nothing_and_say_why(self):
        rep = demo_runner.run("You are repairing ...", prompts.REPAIR_SCHEMA).output
        self.assertEqual((rep["bullets"], rep["skills"], rep["summary"]), ([], [], None))
        note = demo_runner.run("You are editing ...", prompts.NOTE_SCHEMA).output
        self.assertIn("API key", note["cannot"])


@unittest.skipUnless(render.libreoffice_available(), "LibreOffice not installed")
class EndToEndWithLibreOffice(unittest.TestCase):
    """The real thing, no mocks: demo backend, LibreOffice renders the base and the resume, every check runs."""

    def test_a_demo_resume_is_ready(self):
        with TempEnv({"pdf_engine": "libreoffice", "claude_backend": "demo"}):
            pdf = config.PROFILES / PID / "resumes" / f"{BASE_STEM}.pdf"
            pdf.unlink()                                                    # make the engine draw the base itself
            profiles.reload()
            conn = db.connect()
            fields = server.Handler._validate_job(None, {"profile": PID, "company": "Acme Logistics",
                                                         "role": "Senior Machine Learning Engineer", "jd_text": JOB},
                                                  set(profiles.ids()))
            jid = db.add_job(conn, *fields)
            db.claim(conn, jid, str(config.RUNS / str(jid)))
            status = pipeline.process(conn, db.get_job(conn, jid), config.settings())
            job = db.get_job(conn, jid)
            res = db.result_of(job)
            self.assertEqual(status, "ready", job["error"])
            self.assertEqual((job["workplace"], job["location"]), ("hybrid", "Seattle, WA"))
            self.assertGreater(res["ats"]["tailored_pct"], res["ats"]["base_pct"])
            self.assertEqual(res["engine"], "libreoffice")
            self.assertIn("Seattle, WA", docxio.structure(docxio.read(res["files"]["docx"])).contact)
            for key in ("pdf", "docx", "png", "base_pdf"):
                self.assertTrue(Path(res["files"][key]).exists(), key)
            conn.close()


class SetupApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv().__enter__()
        cls.app = server.App()
        cls.httpd = server.make_server(cls.app, 0, "127.0.0.1")
        cls.port = cls.httpd.server_address[1]
        cls.app.port = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = config.TOKEN_PATH.read_text().strip()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.__exit__(None, None, None)

    def call(self, path, body=None, token=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method="POST" if data is not None else "GET")
        if token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_status_lists_every_row_and_no_secret(self):
        config.save_secret("anthropic_api_key", "sk-ant-api03-" + "s" * 30)
        status, body = self.call("/api/setup")
        self.assertEqual(status, 200)
        for key in ("claude", "pdf", "google", "profiles", "worker"):
            self.assertIn(body[key]["state"], ("ok", "missing", "failing", "optional", "demo"), key)
        self.assertEqual(body["claude"]["key"], "sk-ant-...ssss")
        self.assertNotIn("s" * 30, json.dumps(body))
        self.assertEqual(body["google"]["state"], "optional")
        self.assertEqual(self.call("/api/setup", token=False)[0], 401)
        config.save_secret("anthropic_api_key", None)

    def test_saving_a_key_checks_its_shape(self):
        self.assertEqual(self.call("/api/setup/anthropic_key", {"key": "hello"})[0], 400)
        status, body = self.call("/api/setup/anthropic_key", {"key": "sk-ant-api03-" + "k" * 30})
        self.assertEqual((status, body["claude"]["state"]), (200, "ok"))
        self.assertEqual(config.anthropic_key(), "sk-ant-api03-" + "k" * 30)
        self.call("/api/setup/anthropic_key", {"key": ""})
        self.assertIsNone(config.secrets().get("anthropic_api_key"))

    def test_google_client_then_the_consent_redirect(self):
        self.assertEqual(self.call("/api/setup/google_client", {"client_id": "x", "client_secret": "y"})[0], 400)
        status, body = self.call("/api/setup/google_client", {"client_id": "9-z.apps.googleusercontent.com",
                                                              "client_secret": "GOCSPX-1"})
        self.assertEqual((status, body["google"]["state"]), (200, "missing"))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/oauth/google/start")
        req.add_header("Authorization", f"Bearer {self.token}")
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                                  {"redirect_request": lambda *a, **k: None}))
        try:
            opener.open(req, timeout=5)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 302)
            where = err.headers["Location"]
        self.assertTrue(where.startswith("https://accounts.google.com/"))
        self.assertIn(f"127.0.0.1%3A{self.port}%2Foauth%2Fgoogle%2Fcallback", where)
        page = self.call("/oauth/google/callback?state=nope&code=x", token=False)[1].decode()
        self.assertIn("Google not connected", page)
        self.call("/api/setup/google_client", {"remove": True})

    def test_tests_report_instead_of_crashing(self):
        status, body = self.call("/api/setup/test", {"what": "google"})
        self.assertEqual((status, body["result"]["ok"]), (200, False))
        self.assertIn("Settings", body["result"]["error"])
        self.assertEqual(self.call("/api/setup/test", {"what": "nonsense"})[0], 400)

    def test_profile_files_round_trip_and_validation(self):
        status, body = self.call(f"/api/profiles/{PID}/files")
        self.assertEqual((status, body["name"]), (200, "Alex Rivera"))
        self.assertIn("era_jobs", body["files"]["rules.toml"])
        self.assertEqual(self.call(f"/api/profiles/{PID}/file", {"name": "rules.toml", "text": "bullet_words = 3x"})[0], 400)
        self.assertEqual(self.call(f"/api/profiles/{PID}/file", {"name": "../secrets.json", "text": "{}"})[0], 400)
        text = body["files"]["known_skills.txt"] + "Quarkus\n"
        self.assertEqual(self.call(f"/api/profiles/{PID}/file", {"name": "known_skills.txt", "text": text})[0], 200)
        self.assertIn("Quarkus", profiles.load(PID).known_skills)

    def test_new_profile_and_a_resume_upload(self):
        status, body = self.call("/api/profiles", {"id": "jo-park", "name": "Jo Park"})
        self.assertEqual((status, body["profile"]), (201, "jo-park"))
        self.assertEqual(self.call("/api/profiles", {"id": "Bad Id!", "name": "x"})[0], 400)
        bad = base64.b64encode(b"not a docx").decode()
        self.assertEqual(self.call("/api/profiles/jo-park/resume", {"filename": "r.docx", "data": bad})[0], 400)
        good = base64.b64encode((config.EXAMPLES / PID / "resumes" / f"{BASE_STEM}.docx").read_bytes()).decode()
        status, body = self.call("/api/profiles/jo-park/resume", {"filename": "Jo Park resume.docx", "data": good})
        self.assertEqual((status, body["file"]), (200, "Jo_Park_resume.docx"))
        self.assertIn("jo-park", profiles.ids())

    def test_demo_install_turns_on_demo_mode_without_a_key(self):
        status, body = self.call("/api/demo/install", {})
        self.assertEqual((status, body["profile"], body["claude_backend"]), (200, PID, "demo"))
        self.call("/api/settings", {"claude_backend": "api"})

    def test_state_says_whether_claude_is_ready(self):
        self.call("/api/settings", {"claude_backend": "api"})
        body = self.call("/api/state?profile=auto")[1]
        self.assertEqual(body["setup"], {"claude_ready": False, "demo": False})
        self.call("/api/settings", {"claude_backend": "demo"})
        self.assertTrue(self.call("/api/state?profile=auto")[1]["setup"]["claude_ready"])
        self.call("/api/settings", {"claude_backend": "api"})


class NoKeyMeansWaiting(unittest.TestCase):
    def test_a_job_waits_with_a_plain_message_until_a_key_arrives(self):
        from resume_tailor import worker
        with TempEnv({"claude_backend": "api"}):
            conn = db.connect()
            jid = db.add_job(conn, PID, "Acme", "ML Engineer", JD_TEXT)
            w = worker.Worker()
            w._dispatch()
            job = db.get_job(conn, jid)
            self.assertEqual(job["status"], "waiting")
            self.assertIn("API key", job["error"])
            conn.close()
            w.conn.close()


if __name__ == "__main__":
    unittest.main()
