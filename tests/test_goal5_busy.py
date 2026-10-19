"""follow-up: a pressed Re-import / Fix with note / Undo my edit / Cancel / Approve again /
Regenerate stays busy ("Re-importing…", disabled, spinner) until its result is on screen, not only until the API
answers. tests/ui/goal5_busy.mjs holds back the new PDF / page image to prove the old view never looks finished; this
harness plays the worker finishing the note round."""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from resume_tailor import config, db, docedit, server, versions
from tests.helpers import JD_TEXT, TempEnv
from tests.test_goal5_ui import CHROME, _files

DRIVER = Path(__file__).resolve().parent / "ui" / "goal5_busy.mjs"
RUN_TIMEOUT_S = 150
NOTE_DONE = "2026-10-12T22:40:00Z"
REIMPORT_S = 1.0
OLD = 'the summary must open with "AI Engineer with 3+ years"'
NEW = "the contact line says OP, KS; the posting asks for the full city name"


def _needs_you(conn, company: str) -> int:
    jid = db.add_job(conn, "alex-rivera", company, "Data Engineer", JD_TEXT)
    db.update(conn, jid, attempts=1, result=json.dumps({"files": _files(jid, "attempt1"), "attempts": [
        {"n": 1, "kind": "full", "finished": "2026-10-12T15:39:00Z", "failures": ["2 pages"]}],
        "checks": {"ok": False, "failures": []}}))
    db.set_status(conn, jid, "needs_you", error="2 pages")
    return jid


def _fake_reimport(keep: set):
    """The re-import the server runs (its real endpoint is called): about a second, then the new version. A job in
    `keep` had only problems from before the edit (it stays ready); any other gets one new problem from the edit."""
    def reimport(conn, job, docx_bytes, settings=None, google=None, doc_meta=None, doc_pdf=None):
        time.sleep(REIMPORT_S)
        jid, kept = job["id"], job["id"] in keep
        stamp = f"2026-10-12T23:0{jid % 10}:00Z"
        failures = [{"check": 7, "name": "summary_start", "message": OLD}] + (
            [] if kept else [{"check": 7, "name": "contact_place", "message": NEW}])
        result = db.result_of(job)
        result.update({"files": _files(jid, "reimport2"), "files_at": stamp, "checks": {"ok": False, "failures": failures},
                       "attempts": result.get("attempts", []) + [{"n": 2, "kind": "reimport", "finished": stamp,
                                                                   "failures": [f["message"] for f in failures]}],
                       "reimport": {"n": 2, "changes": ["Location on the top line: OP, KS"], "not_kept": [], "doc": {},
                                    "format_note": "Formatting edits are not kept.", "old_problems": [OLD],
                                    "new_problems": [] if kept else [NEW],
                                    "moved": None if kept else {"from": job["status"], "to": "needs_you"}}})
        db.update(conn, jid, result=json.dumps(result))
        status = "ready" if kept else "needs_you"
        db.set_status(conn, jid, status, "re-import 2", error=None if kept else f"{NEW}; {OLD}")
        return status
    return reimport


def _reimported(conn, company: str = "Co busy") -> int:
    """A ready resume shown after a re-import, with the version before it kept (so Undo my edit has one)."""
    jid = db.add_job(conn, "alex-rivera", company, "AI Engineer", JD_TEXT)
    db.update(conn, jid, attempts=1, result=json.dumps({"files": _files(jid, "attempt1"), "attempts": [
        {"n": 1, "kind": "full", "finished": "2026-10-12T15:39:00Z", "failures": []}]}))
    db.set_status(conn, jid, "ready")
    versions.snapshot(db.get_job(conn, jid), "reimport", "reimport1")
    db.update(conn, jid, gdoc_id="D1", gdoc_link="https://docs.example/D1", result=json.dumps({
        "files": _files(jid, "reimport1"), "files_at": "2026-10-12T18:35:10Z",
        "attempts": [{"n": 1, "kind": "full", "finished": "2026-10-12T15:39:00Z", "failures": []},
                     {"n": 1, "kind": "reimport", "finished": "2026-10-12T18:35:10Z", "failures": []}],
        "reimport": {"n": 1, "changes": ["Location on the top line: Phoenix, AZ"], "not_kept": [], "doc": {},
                     "format_note": "Formatting edits in the Doc (bold, spacing, fonts) are not kept."}}))
    db.set_status(conn, jid, "ready")
    return jid


def _finish_note(conn, jid: int) -> None:
    """What the worker writes when the note round ends (one Claude call, problems listed)."""
    result = db.result_of(db.get_job(conn, jid))
    result.update({"files": _files(jid, "attempt2"), "files_at": NOTE_DONE,
                   "attempts": result.get("attempts", []) + [{"n": 2, "kind": "fix", "finished": NOTE_DONE, "failures": ["x"]}],
                   "fix": {"note": "I know data warehousing", "applied": True, "saved": ["Data Warehousing"],
                           "cannot": None, "changes": ["Added Data Warehousing to Skills"]}})
    db.update(conn, jid, attempts=2, result=json.dumps(result))
    db.set_status(conn, jid, "needs_you", "note round done", error="widow")


@unittest.skipUnless(shutil.which("node") and CHROME, "needs node and Google Chrome")
class Goal5BusyTests(unittest.TestCase):
    def test_buttons_stay_busy_until_the_result_is_on_screen(self):
        with TempEnv() as env:
            app = server.App()
            httpd = server.make_server(app, 0, "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            profile_dir = tempfile.mkdtemp(prefix="rg-chrome-")
            try:
                ids = {"v_ready": _reimported(app.conn), "v_keep": _reimported(app.conn, "Co keep"),
                       "v_fix": _needs_you(app.conn, "Co fix"), "v_drop": _needs_you(app.conn, "Co drop")}
                patches = [mock.patch.object(docedit, "reimport", _fake_reimport({ids["v_keep"]})),
                           mock.patch.object(app, "with_google", side_effect=lambda pid, fn: (b"", {}))]
                for p in patches:
                    p.start()
                self.addCleanup(lambda: [p.stop() for p in patches])
                token = config.TOKEN_PATH.read_text().strip()
                proc = subprocess.Popen(["node", str(DRIVER), f"http://127.0.0.1:{httpd.server_address[1]}", token,
                                         CHROME, profile_dir, json.dumps(ids)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                timer = threading.Timer(RUN_TIMEOUT_S, proc.kill)
                timer.start()
                lines, fix_status = [], None
                for line in proc.stdout:
                    lines.append(line)
                    if line.strip() == "FINISH_FIX":
                        fix_status = db.get_job(app.conn, ids["v_fix"])["status"]
                        _finish_note(app.conn, ids["v_fix"])
                        proc.stdin.write("done\n")
                        proc.stdin.flush()
                proc.wait()
                timer.cancel()
                err = proc.stderr.read()
                jobs = {k: db.get_job(app.conn, v) for k, v in ids.items()}
            finally:
                httpd.shutdown()
                httpd.server_close()
                shutil.rmtree(profile_dir, ignore_errors=True)
        out = [ln for ln in lines if ln.startswith("{")]
        self.assertTrue(out, f"driver printed nothing: {err[-500:]}")
        checks = json.loads(out[-1])["checks"]
        failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
        self.assertGreaterEqual(len(checks), 20)
        self.assertEqual(failed, [])
        self.assertEqual(fix_status, "queued")                      # the note was sent to the worker for real
        self.assertEqual(jobs["v_drop"]["status"], "cancelled")      # Cancel: never uploaded…
        self.assertTrue(jobs["v_drop"]["archived_at"])               # …in Archived, restorable as needs you
        self.assertEqual(db.result_of(jobs["v_drop"])["dropped"]["from"], "needs_you")
        self.assertEqual(jobs["v_ready"]["status"], "needs_you")     # the edit with a new problem
        self.assertEqual(jobs["v_keep"]["status"], "queued")         # Regenerate queued it for Claude
        self.assertEqual(jobs["v_fix"]["status"], "ready")           # Looks good (phone) accepted it
