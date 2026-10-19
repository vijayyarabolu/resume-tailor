"""in a real browser (tests/ui/goal20_rg.mjs): × Delete for good on an archived resume (press twice, gone at
once, Undo), Clear archived (asks with the number, one person only), the auto-clear days field, phone width."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from resume_tailor import config, db, server
from tests.helpers import CHROME, JD_TEXT, TempEnv

DRIVER = Path(__file__).resolve().parent / "ui" / "goal20_rg.mjs"
RUN_TIMEOUT_S = 150


def _seed(conn, tmp: Path) -> dict:
    pdf = tmp / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ids = {}
    for key, profile in (("a1", "alex-rivera"), ("a2", "alex-rivera"), ("a3", "alex-rivera"), ("s1", "sam-lee"), ("live", "alex-rivera")):
        jid = db.add_job(conn, profile, f"Co {key}", "ML Engineer", JD_TEXT)
        db.update(conn, jid, result=json.dumps({"files": {"pdf": str(pdf)}, "base": {"id": "ml_engineer", "label": "ML"}}),
                  attempts=1)
        db.set_status(conn, jid, "ready")
        if key != "live":
            db.archive(conn, jid)
        ids[key] = jid
    return ids


@unittest.skipUnless(shutil.which("node") and CHROME, "needs node and Chrome or Chromium")
class Goal20ArchivedUiTests(unittest.TestCase):
    def test_delete_for_good_and_clear_archived_in_the_browser(self):
        with TempEnv() as env:
            app = server.App()
            httpd = server.make_server(app, 0, "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            profile_dir = tempfile.mkdtemp(prefix="rg-chrome-")
            downloads = tempfile.mkdtemp(prefix="rg-downloads-")
            try:
                ids = _seed(app.conn, env.tmp)
                token = config.TOKEN_PATH.read_text().strip()
                out = subprocess.run(["node", str(DRIVER), f"http://127.0.0.1:{httpd.server_address[1]}", token, CHROME,
                                      profile_dir, json.dumps(ids), "{}", downloads],
                                     capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
                live = db.get_job(app.conn, ids["live"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                shutil.rmtree(profile_dir, ignore_errors=True)
                shutil.rmtree(downloads, ignore_errors=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, f"driver printed nothing: {out.stderr[-500:]}")
        checks = json.loads(lines[-1])["checks"]
        failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
        self.assertGreaterEqual(len(checks), 13)
        self.assertEqual(failed, [])
        self.assertIsNone(live["deleting_at"], "Clear archived touched a resume that was not archived")


if __name__ == "__main__":
    unittest.main()
