"""in a real browser (tests/ui/goal4_ui.mjs): the Alex | Sam switch on every tab with the right counts,
remembered per browser, ?profile= links, day groups with numbered rows and times,
approved / downloaded stamps, Archive all approved for one person, phone width."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from resume_tailor import config, db, server
from tests.helpers import CHROME, JD_TEXT, TempEnv

DRIVER = Path(__file__).resolve().parent / "ui" / "goal4_ui.mjs"
RUN_TIMEOUT_S = 150
LOCAL = datetime.now().astimezone().tzinfo           # the browser groups days in the same time zone
QUIET_AFTER_MIDNIGHT_MIN = 10


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_title(when: datetime) -> str:
    d = when.astimezone(LOCAL)
    return f"{d:%a} {d:%b} {d.day}"


def _seed(conn, tmp: Path) -> tuple[dict, dict]:
    pdf = tmp / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    now = datetime.now(timezone.utc)
    ids = {}

    def add(key, profile, status, made=None, approved=None, archived=False):
        jid = db.add_job(conn, profile, f"Co {key}", "ML Engineer", JD_TEXT)
        result = {"files": {"pdf": str(pdf)}, "base": {"id": "ml_engineer", "label": "ML Engineer"}}
        if approved:
            result["approved"] = {"file_id": f"d{jid}", "link": "https://drive.example/x", "name": "x.pdf",
                                  "at": _iso(approved)}
        if status != "queued":
            db.update(conn, jid, result=json.dumps(result), attempts=1)
            db.set_status(conn, jid, status)
        if made:
            db.update(conn, jid, finished_at=_iso(made))
        if approved:
            db.update(conn, jid, drive_file_id=f"d{jid}", drive_link="https://drive.example/x", drive_name="x.pdf")
        if archived:
            db.archive(conn, jid)
        ids[key] = jid

    add("v_today1", "alex-rivera", "ready", made=now - timedelta(minutes=3))
    add("v_today2", "alex-rivera", "ready", made=now - timedelta(minutes=2), approved=now - timedelta(minutes=1))
    add("v_yday", "alex-rivera", "ready", made=now - timedelta(days=1))
    add("v_3d", "alex-rivera", "ready", made=now - timedelta(days=3))
    add("v_old", "alex-rivera", "ready", made=now - timedelta(days=40))
    add("v_live", "alex-rivera", "queued")
    add("v_arch", "alex-rivera", "ready", made=now - timedelta(minutes=5), archived=True)
    add("s_today", "sam-lee", "ready", made=now - timedelta(minutes=4), approved=now - timedelta(minutes=1))
    add("s_live", "sam-lee", "queued")
    add("s_needs", "sam-lee", "needs_you", made=now - timedelta(minutes=6))
    add("s_arch", "sam-lee", "ready", made=now - timedelta(minutes=7), archived=True)
    expect = {"today": f"Today · {_day_title(now)}", "yday": f"Yesterday · {_day_title(now - timedelta(days=1))}",
              "d3": _day_title(now - timedelta(days=3)), "approved": datetime.fromtimestamp(
                  (now - timedelta(minutes=1)).timestamp(), LOCAL).strftime("%-I:%M %p")}
    return ids, expect


@unittest.skipUnless(shutil.which("node") and CHROME, "needs node and Chrome or Chromium")
class Goal4UiTests(unittest.TestCase):
    def test_profiles_and_day_groups_in_the_browser(self):
        if datetime.now(LOCAL).hour == 0 and datetime.now(LOCAL).minute < QUIET_AFTER_MIDNIGHT_MIN:
            self.skipTest("the 'today' rows are a few minutes old and would land on yesterday")
        with TempEnv() as env:
            app = server.App()
            httpd = server.make_server(app, 0, "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            profile_dir = tempfile.mkdtemp(prefix="rg-chrome-")
            downloads = tempfile.mkdtemp(prefix="rg-downloads-")
            try:
                ids, expect = _seed(app.conn, env.tmp)
                token = config.TOKEN_PATH.read_text().strip()
                out = subprocess.run(["node", str(DRIVER), f"http://127.0.0.1:{httpd.server_address[1]}", token, CHROME,
                                      profile_dir, json.dumps(ids), json.dumps(expect), downloads],
                                     capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
                alex_approved = db.get_job(app.conn, ids["v_today2"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                shutil.rmtree(profile_dir, ignore_errors=True)
                shutil.rmtree(downloads, ignore_errors=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, f"driver printed nothing: {out.stderr[-500:]}")
        checks = json.loads(lines[-1])["checks"]
        failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
        self.assertGreaterEqual(len(checks), 25)
        self.assertEqual(failed, [])
        self.assertIsNone(alex_approved["archived_at"], "Sam's Archive all approved touched Alex's resume")


if __name__ == "__main__":
    unittest.main()
