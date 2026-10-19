"""in a real browser (tests/ui/goal5_ui.mjs): after a Fix with note or a re-import the card / panel says what
changed and lists the problems, offers Looks good / Fix with note / Open as Google Doc / Undo my edit / Cancel, file
URLs carry the version, Cancel asks twice and can be undone, keys are listed under "?", both people, phone width."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from resume_tailor import config, db, server, versions
from tests.helpers import CHROME, JD_TEXT, TempEnv

DRIVER = Path(__file__).resolve().parent / "ui" / "goal5_ui.mjs"
RUN_TIMEOUT_S = 150
NOTE = "I know data warehousing make it fit naturally where ever that fits naturally"


def _files(jid: int, folder: str) -> dict:
    where = config.RUNS / str(jid) / folder
    where.mkdir(parents=True, exist_ok=True)
    out = {}
    for key, name in (("pdf", "resume.pdf"), ("png", "page-1.png"), ("docx", "resume.docx")):
        (where / name).write_bytes(f"{folder} {name}".encode())
        out[key] = str(where / name)
    return out


def _after_note(conn, profile: str) -> tuple:
    jid = db.add_job(conn, profile, f"Co {profile} note", "Data Engineer", JD_TEXT)
    db.update(conn, jid, attempts=1, result=json.dumps({"files": _files(jid, "attempt1"), "attempts": [
        {"n": 1, "kind": "full", "finished": "2026-10-12T15:39:00Z", "failures": []}]}))
    db.set_status(conn, jid, "ready")
    versions.snapshot(db.get_job(conn, jid), "fix")              # what the server keeps when the note is sent
    stamp = "2026-10-12T15:44:14Z"
    result = {"files": _files(jid, "attempt2"), "files_at": stamp, "attempts": [
        {"n": 1, "kind": "full", "finished": "2026-10-12T15:39:00Z", "failures": []},
        {"n": 2, "kind": "fix", "finished": stamp, "failures": ["x"]}],
        "fix": {"note": NOTE, "applied": True, "saved": ["Data Warehousing"], "cannot": None,
                "changes": ["Added Data Warehousing to Skills (Data Engineering)", "Northwind Analytics bullet 3"]},
        "checks": {"ok": False, "failures": [{"check": 4, "name": "widow", "message":
                   "bullet 3 of role 3 (Northwind Analytics, India) is a widow: its last line fills 12% of the width"}]}}
    db.update(conn, jid, attempts=2, result=json.dumps(result))
    db.set_status(conn, jid, "needs_you", error="bullet 3 of role 3 is a widow")
    return jid, stamp


def _seed(conn) -> tuple:
    ids, want = {}, {"note": NOTE}
    ids["v_note"], want["v_note_version"] = _after_note(conn, "alex-rivera")
    ids["s_note"], _ = _after_note(conn, "sam-lee")
    plain = db.add_job(conn, "alex-rivera", "Co plain", "R", JD_TEXT)
    db.update(conn, plain, result=json.dumps({"files": _files(plain, "attempt1"), "checks": {"failures": []}}))
    db.set_status(conn, plain, "needs_you", error="2 pages")
    ids["v_plain"] = plain
    doc = db.add_job(conn, "alex-rivera", "Co doc", "AI Agent Developer", JD_TEXT)
    stamp = want["v_doc_version"] = "2026-10-12T18:35:10Z"
    db.update(conn, doc, drive_file_id="F1", drive_link="https://drive.example/F1", drive_name="x.pdf", result=json.dumps({
        "files": _files(doc, "reimport1"), "files_at": stamp,
        "approved": {"file_id": "F1", "at": "2026-10-12T18:22:29Z", "pdf": "/old/attempt3/resume.pdf"},
        "attempts": [{"n": 3, "kind": "repair", "finished": "2026-10-12T18:21:50Z", "failures": []},
                     {"n": 1, "kind": "reimport", "finished": stamp, "failures": []}],
        "reimport": {"n": 1, "changes": ["Location on the top line: Phoenix, AZ"], "not_kept": [],
                     "doc": {"modifiedTime": "2026-10-12T18:34:00Z"},
                     "format_note": "Formatting edits in the Doc (bold, spacing, fonts) are not kept."}}))
    db.set_status(conn, doc, "ready")
    ids["v_doc"] = doc
    return ids, want


@unittest.skipUnless(shutil.which("node") and CHROME, "needs node and Chrome or Chromium")
class Goal5UiTests(unittest.TestCase):
    def test_edit_results_undo_cancel_and_keys_in_the_browser(self):
        with TempEnv() as env:
            app = server.App()
            httpd = server.make_server(app, 0, "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            profile_dir = tempfile.mkdtemp(prefix="rg-chrome-")
            try:
                ids, want = _seed(app.conn)
                token = config.TOKEN_PATH.read_text().strip()
                out = subprocess.run(["node", str(DRIVER), f"http://127.0.0.1:{httpd.server_address[1]}", token, CHROME,
                                      profile_dir, json.dumps(ids), json.dumps(want)],
                                     capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
                note_job = db.get_job(app.conn, ids["v_note"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                shutil.rmtree(profile_dir, ignore_errors=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, f"driver printed nothing: {out.stderr[-500:]}")
        checks = json.loads(lines[-1])["checks"]
        failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
        self.assertGreaterEqual(len(checks), 25)
        self.assertEqual(failed, [])
        self.assertEqual(note_job["status"], "ready")                  # Undo my edit brought the ready version back
        self.assertTrue(db.result_of(note_job)["files"]["pdf"].endswith("resume.pdf"))


if __name__ == "__main__":
    unittest.main()
