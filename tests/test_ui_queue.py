"""in a real browser: the Queue shows only live + needs-you work, a needs-you card has the page image,
plain reasons, ATS, the diff, Looks good and Fix with note; Clear and Archive work (tests/ui/queue_ui.mjs)."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from resume_tailor import config, db, server
from tests.helpers import CHROME, JD_TEXT, TempEnv, example_profile

DRIVER = Path(__file__).resolve().parent / "ui" / "queue_ui.mjs"
RUN_TIMEOUT_S = 120
PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8ff"
                        "ff3f0005fe02fea7d6a4810000000049454e44ae426082")
DIFF = {"headline": {"changed": False, "new": "Machine Learning Engineer"},
        "summary": {"changed": True, "base": "old summary", "new": "new summary", "ops": [["replace", "old", "new"],
                                                                                         ["equal", "summary", ""]]},
        "skills": [], "jobs": []}


def _seed(conn, tmp: Path) -> dict:
    png = tmp / "page-1.png"
    png.write_bytes(PNG_1PX)
    files = {"pdf": str(tmp / "resume.pdf"), "png": str(png)}
    (tmp / "resume.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    failure = {"check": 7, "name": "bullet_words", "message": "bullet 5 of role 1 (Lumen Freight, USA) has 26 "
                                                              "words; the rule is 15 to 25"}
    _, base = example_profile()
    files["base_pdf"] = str(base.pdf)
    result = {"files": files, "ats": {"base_pct": 70, "tailored_pct": 75}, "diff": DIFF,
              "checks": {"ok": False, "failures": [failure]}, "base": {"label": "ML Engineer", "id": "ml_engineer"}}
    ids = {}

    def add(key, status, own=None, **extra):
        jid = db.add_job(conn, "alex-rivera", f"Co {key}", "ML Engineer", JD_TEXT)
        if status != "queued":
            db.update(conn, jid, result=json.dumps(own or result), attempts=2)
            db.set_status(conn, jid, status, error=failure["message"] if status in ("needs_you", "failed") else None)
        if extra:
            db.update(conn, jid, **extra)
        ids[key] = jid
    add("needs_a", "needs_you")
    add("needs_b", "needs_you")
    add("live", "queued")
    add("ready", "ready")
    add("approved", "ready", drive_file_id="drive-1", drive_link="https://drive.example/1", drive_name="x.pdf")
    old = {**result, "files": {**files, "base_pdf": "/x/resumes/archive/Alex_Rivera_ML_Engineer.pdf"}}
    add("stale", "ready", own=old, finished_at="2026-10-05T15:00:00Z")      # made from an archived base, 2 days old
    add("cancelled", "cancelled")
    add("failed", "failed")
    return ids


@unittest.skipUnless(shutil.which("node") and CHROME, "needs node and Chrome or Chromium")
class QueueUiTests(unittest.TestCase):
    def test_review_and_clear_in_the_browser(self):
        with TempEnv() as env:
            app = server.App()
            httpd = server.make_server(app, 0, "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                ids = _seed(app.conn, env.tmp)
                token = config.TOKEN_PATH.read_text().strip()
                profile_dir = tempfile.mkdtemp(prefix="rg-chrome-")
                out = subprocess.run(["node", str(DRIVER), f"http://127.0.0.1:{httpd.server_address[1]}", token, CHROME,
                                      profile_dir, json.dumps(ids)], capture_output=True, text=True,
                                     timeout=RUN_TIMEOUT_S)
                shutil.rmtree(profile_dir, ignore_errors=True)
            finally:
                httpd.shutdown()
                httpd.server_close()
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, f"driver printed nothing: {out.stderr[-500:]}")
        checks = json.loads(lines[-1])["checks"]
        failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
        self.assertGreaterEqual(len(checks), 38)
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
