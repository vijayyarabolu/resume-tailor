"""In a real browser (tests/ui/goal21_rg.mjs): the app shell (top bar, side menu, person switch, theme, worker
status), Settings as its own view showing the model in use, the tracker link, and the phone (a bottom bar, the
review panel inside 390 px)."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from resume_tailor import db, server
from resume_tailor import config
from tests.helpers import CHROME, JD_TEXT, TempEnv

DRIVER = Path(__file__).resolve().parent / "ui" / "goal21_rg.mjs"
RUN_TIMEOUT_S = 150
LONG_DRIVE_NAME = "Alex_Rivera_Machine_Learning_Engineer_Resume(Some Very Long Company Name Incorporated - Staff ML).pdf"


def _seed(conn, tmp: Path) -> dict:
    pdf = tmp / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ids = {}
    for key, profile in (("long", "alex-rivera"), ("v2", "alex-rivera"), ("s1", "sam-lee")):
        jid = db.add_job(conn, profile, f"Co {key}", "ML Engineer", JD_TEXT)
        db.update(conn, jid, result=json.dumps({"files": {"pdf": str(pdf)}, "base": {"id": "ml_engineer", "label": "ML"}}), attempts=1)
        db.set_status(conn, jid, "ready")
        ids[key] = jid
    db.update(conn, ids["long"], drive_link="https://drive.example/f1", drive_name=LONG_DRIVE_NAME, gdoc_link="https://docs.example/d1")
    return ids


@unittest.skipUnless(shutil.which("node") and CHROME, "needs node and Chrome or Chromium")
class Goal21ShellUiTests(unittest.TestCase):
    def test_the_shared_shell_in_the_browser(self):
        with TempEnv({"model": "claude-sonnet-5-5"}) as env:
            app = server.App()
            httpd = server.make_server(app, 0, "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            profile_dir = tempfile.mkdtemp(prefix="rg-chrome-")
            try:
                ids = _seed(app.conn, env.tmp)
                token = config.TOKEN_PATH.read_text().strip()
                out = subprocess.run(["node", str(DRIVER), f"http://127.0.0.1:{httpd.server_address[1]}", token, CHROME,
                                      profile_dir, json.dumps(ids), "{}", profile_dir],
                                     capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
                settings = json.loads(config.SETTINGS_PATH.read_text())
            finally:
                httpd.shutdown()
                httpd.server_close()
                shutil.rmtree(profile_dir, ignore_errors=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, f"driver printed nothing: {out.stderr[-500:]}")
        checks = json.loads(lines[-1])["checks"]
        failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
        self.assertGreaterEqual(len(checks), 22)
        self.assertEqual(failed, [])
        self.assertEqual(settings.get("model"), "claude-sonnet-5-5", "the browser changed the model")


if __name__ == "__main__":
    unittest.main()
