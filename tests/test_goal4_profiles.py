"""Server side: whose page opens first, the "downloaded" stamp, per-person archive."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from resume_tailor import config, db, server
from tests.helpers import JD_TEXT, TempEnv

class StateAndFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv(people=("alex-rivera", "sam-lee")).__enter__()
        cls.app = server.App()
        cls.httpd = server.make_server(cls.app, 0, "127.0.0.1")
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = config.TOKEN_PATH.read_text().strip()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.__exit__(None, None, None)

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method="POST" if data is not None else "GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def ready_job(self, profile: str, approved: bool = False) -> int:
        conn = self.app.conn
        pdf = self.env.tmp / f"resume-{profile}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        jid = db.add_job(conn, profile, "Acme", "ML Engineer", JD_TEXT)
        db.update(conn, jid, result=json.dumps({"files": {"pdf": str(pdf)}}))
        db.set_status(conn, jid, "ready")
        if approved:
            db.update(conn, jid, drive_file_id=f"drive-{jid}", drive_link="https://drive.example/x", drive_name="x.pdf")
        return jid

    def test_download_link_stamps_downloaded_but_viewing_does_not(self):
        jid = self.ready_job("alex-rivera")
        status, _ = self.call(f"/api/jobs/{jid}/file/pdf/x.pdf")                  # the panel's PDF view
        self.assertEqual(status, 200)
        self.assertIsNone(db.get_job(self.app.conn, jid)["downloaded_at"])
        status, _ = self.call(f"/api/jobs/{jid}/file/pdf/x.pdf?dl=1")             # the Download PDF link
        self.assertEqual(status, 200)
        job = db.get_job(self.app.conn, jid)
        self.assertRegex(job["downloaded_at"] or "", r"^\d{4}-\d\d-\d\dT")
        state = self.call("/api/state")[1]
        self.assertEqual(next(j for j in state["jobs"] if j["id"] == jid)["times"]["downloaded"], job["downloaded_at"])

    def test_state_for_one_person_lists_only_theirs_without_the_shared_cap(self):
        mine, theirs = self.ready_job("sam-lee"), self.ready_job("alex-rivera")
        gone = self.ready_job("sam-lee")
        db.archive(self.app.conn, gone)
        body = self.call("/api/state?profile=sam-lee")[1]
        self.assertEqual({j["profile"] for j in body["jobs"] + body["archived"]}, {"sam-lee"})
        self.assertIn(mine, [j["id"] for j in body["jobs"]])
        self.assertIn(gone, [j["id"] for j in body["archived"]])
        self.assertGreater(server.STATE_PERSON_LIMIT, 300)
        everyone = self.call("/api/state?profile=nobody")[1]               # unknown person: the old shared view
        self.assertIn(theirs, [j["id"] for j in everyone["jobs"]])

    def test_regenerate_lists_it_under_today_and_forgets_the_old_download(self):
        jid = self.ready_job("alex-rivera")
        db.update(self.app.conn, jid, created_at="2026-10-01T15:00:00Z", downloaded_at="2026-10-02T15:00:00Z")
        db.requeue(self.app.conn, jid)
        job = db.get_job(self.app.conn, jid)
        self.assertIsNone(job["downloaded_at"])
        self.assertGreater(job["requeued_at"], "2026-10-02")

    def test_the_workers_own_requeue_keeps_the_queue_day_and_download(self):
        jid = self.ready_job("alex-rivera")
        db.update(self.app.conn, jid, downloaded_at="2026-10-02T15:00:00Z", requeued_at="2026-10-03T15:00:00Z")
        db.requeue(self.app.conn, jid, keep_attempts=True)                  # unpark / usage-limit pause
        job = db.get_job(self.app.conn, jid)
        self.assertEqual((job["requeued_at"], job["downloaded_at"]), ("2026-10-03T15:00:00Z", "2026-10-02T15:00:00Z"))

    def test_state_auto_answers_for_the_default_person_else_the_first(self):
        self.ready_job("sam-lee"), self.ready_job("alex-rivera")
        self.app.settings = {**self.app.settings, "default_profile": "sam-lee"}
        body = self.call("/api/state?profile=auto")[1]
        self.assertEqual((body["profile"], {j["profile"] for j in body["jobs"]}), ("sam-lee", {"sam-lee"}))
        self.app.settings = {**self.app.settings, "default_profile": None}
        body = self.call("/api/state?profile=auto")[1]
        self.assertEqual((body["profile"], {j["profile"] for j in body["jobs"]}), ("alex-rivera", {"alex-rivera"}))
        self.assertIsNone(self.call("/api/state")[1]["profile"])

    def test_archive_all_approved_takes_only_the_chosen_person(self):
        mine, theirs = self.ready_job("sam-lee", approved=True), self.ready_job("alex-rivera", approved=True)
        _, body = self.call("/api/archive", {"what": "approved", "profile": "sam-lee"})
        self.assertIn(mine, body["archived"])
        self.assertNotIn(theirs, body["archived"])
        self.assertIsNone(db.get_job(self.app.conn, theirs)["archived_at"])


if __name__ == "__main__":
    unittest.main()
