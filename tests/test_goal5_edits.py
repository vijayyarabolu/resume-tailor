"""/: a re-imported Google Doc edit is always shown (its contact-line city kept, Google's skills lines read
right, what the layout cannot carry said), the Doc is read at its latest revision, Approve sends the edited PDF,
and Undo my edit / Cancel work."""
import json
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from resume_tailor import changes, config, db, docedit, docxio, pdfx, pipeline, reasons, render, server, versions
from tests.helpers import JD_TEXT, TempEnv, example_profile
from tests.test_pipeline import _ok_result

TOOLS = not pdfx.tools_missing()
W = docxio.W_NS
HOME = "Portland, OR"


def _para(runs: str) -> ET.Element:
    return ET.fromstring(f'<w:p xmlns:w="{W}">{runs}</w:p>')


def _bold(text: str, br_before=False, br_after=False) -> str:
    return (f'<w:r><w:rPr><w:b/></w:rPr>{"<w:br/>" if br_before else ""}<w:t xml:space="preserve">{text}</w:t>'
            f'{"<w:br/>" if br_after else ""}</w:r>')


def _plain(text: str, br_after=False) -> str:
    return f'<w:r><w:t>{text}</w:t>{"<w:br/>" if br_after else ""}</w:r>'


class GoogleSkillsLinesTests(unittest.TestCase):
    def test_a_break_after_the_text_ends_the_line_and_one_before_it_starts_the_next(self):
        google = _para(_bold("Vector: ") + _plain("Pinecone, Neo4j", br_after=True) + _bold("MLOps: ")
                       + _plain("LLMOps, Docker") + _bold("Cloud: ", br_before=True) + _plain("AWS"))
        groups = [docxio._group_parts(g) for g in docxio.skill_groups(google)]
        self.assertEqual(groups, [("Vector", "Pinecone, Neo4j"), ("MLOps", "LLMOps, Docker"), ("Cloud", "AWS")])

    def test_word_breaks_in_their_own_run_still_split(self):
        word = _para(_bold("A: ") + _plain("x") + '<w:r><w:br/></w:r>' + _bold("B: ") + _plain("y"))
        self.assertEqual([docxio._group_parts(g) for g in docxio.skill_groups(word)], [("A", "x"), ("B", "y")])


class ChangesTests(unittest.TestCase):
    def test_contact_line_edits(self):
        shown = "555-010-0199 | me@x.com | Virtual, AZ | LinkedIn | GitHub"
        self.assertEqual(changes.contact_change(shown, shown.replace("Virtual", "Phoenix"), "Virtual, AZ"),
                         ("Phoenix, AZ", [], None))
        self.assertEqual(changes.contact_change(shown, shown.replace("555-010-0199", "555-010-0000"), "Virtual, AZ"),
                         (None, [["555-010-0199", "555-010-0000"]], None))
        loc, edits, problem = changes.contact_change(shown, "me@x.com | Phoenix", "Virtual, AZ")
        self.assertIn("could not be placed", problem)

    def test_describe_in_plain_words(self):
        old = {"summary": "a b c d", "skills": [{"category": "Data", "items": ["SQL"]}],
               "jobs": [{"company": "Northwind Analytics, India", "bullets": ["x", "y", "z"]}]}
        new = {"summary": "a b", "skills": [{"category": "Data", "items": ["SQL", "Data Warehousing"]}],
               "jobs": [{"company": "Northwind Analytics, India", "bullets": ["x", "y", "z2"]}], "user_location": "Phoenix, AZ"}
        self.assertEqual(changes.describe(old, new), ["Location on the top line: Phoenix, AZ",
                                                      "Summary shortened (4 → 2 words)",
                                                      "Added Data Warehousing to Skills (Data)", "Northwind Analytics bullet 3"])


class FakeGoogle:
    """export() answers from a list (one per call); get() reports versions from a list."""

    def __init__(self, exports, versions_, created="2026-10-12T18:23:00Z", modified="2026-10-12T18:34:00Z"):
        self.exports, self.versions, self.calls = list(exports), list(versions_), []
        self.created, self.modified = created, modified

    def get(self, file_id, fields=None):
        v = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        return {"version": v, "createdTime": self.created, "modifiedTime": self.modified}

    def export(self, file_id, mime):
        self.calls.append(mime)
        return self.exports.pop(0) if len(self.exports) > 1 else self.exports[0]


class ExportLatestTests(unittest.TestCase):
    def setUp(self):
        _, base = example_profile()
        self.shown = base.docx.read_bytes()
        content = base.structure.as_content()
        content["summary"] = "Edited by hand. " + content["summary"]
        self.edited = docxio.fill(docxio.read(base.docx), content)

    def test_re_exported_while_drive_still_serves_the_old_text(self):
        g = FakeGoogle([self.shown, self.edited], ["7"])
        sleep = mock.Mock()
        data, meta = docedit.export_latest(g, "doc", self.shown, sleep)
        self.assertEqual(data, self.edited)
        self.assertEqual(len(g.calls), 2)
        sleep.assert_called_once_with(docedit.EXPORT_WAIT_S)
        self.assertEqual(meta, {"modifiedTime": "2026-10-12T18:34:00Z", "version": "7"})

    def test_re_exported_when_the_doc_changed_during_the_export(self):
        g = FakeGoogle([self.shown, self.edited], ["7", "8", "8", "8"])
        data, meta = docedit.export_latest(g, "doc", None, mock.Mock())
        self.assertEqual((data, meta["version"]), (self.edited, "8"))

    def test_an_unedited_doc_is_read_once(self):
        g = FakeGoogle([self.shown], ["3"], modified="2026-10-12T18:23:00Z")
        docedit.export_latest(g, "doc", self.shown, mock.Mock())
        self.assertEqual(len(g.calls), 1)


@unittest.skipUnless(TOOLS, "poppler tools missing")
class ReimportTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()
        self.prof, self.base = example_profile()
        self.content = self.base.structure.as_content()
        self.content.update({"change_notes": ["c"], "match": "m", "review": ["r"], "gaps": [], "added_from_jd": []})
        self.patch = mock.patch.object(render, "to_pdf", side_effect=lambda *a, **k: (self.base.pdf.read_bytes(),
                                                                                       "test"))
        self.patch.start()
        jid = db.add_job(self.conn, "alex-rivera", "Acme", "Machine Learning Engineer", JD_TEXT, base_override="ml_engineer")
        db.update(self.conn, jid, location="Virtual, AZ", workplace="onsite")      # as a tracker sends it
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        runner = mock.Mock(return_value=_ok_result(self.content))
        self.first = pipeline.process(self.conn, db.get_job(self.conn, jid), config.settings(), None, runner)
        self.jid = jid

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _job(self):
        return db.get_job(self.conn, self.jid)

    def _doc(self, place="Phoenix, AZ", title=None, drop_bullet=False) -> bytes:
        """What the person's Google Doc exports after their edit."""
        content = json.loads(json.dumps(self.content))
        content["contact_location"] = (HOME, place)
        doc = docxio.read(docxio.fill(docxio.read(self.base.docx), content))
        struct = docxio.structure(doc)
        paras = {p.index: p.el for p in doc.paras}
        if title:
            docxio.set_para_text(paras[struct.jobs[0]["title_para"]], title)
        if drop_bullet:
            el = paras[struct.jobs[0]["bullet_paras"][-1]]
            for parent in doc.root.iter():
                if el in list(parent):
                    parent.remove(el)
        return docxio.serialize(doc)

    def test_job_111_the_city_edit_is_kept_shown_and_undoable(self):
        before = db.result_of(self._job())
        self.assertIn("Virtual, AZ", docxio.structure(docxio.read(before["files"]["docx"])).contact)
        status = docedit.reimport(self.conn, self._job(), self._doc(), config.settings(), None, {"version": "9"})
        res = db.result_of(self._job())
        shown = docxio.structure(docxio.read(res["files"]["docx"]))
        self.assertIn("Phoenix, AZ", shown.contact)                       # the person's edit, not the job's city
        self.assertNotIn("Virtual", shown.contact)
        self.assertEqual(res["reimport"]["changes"], ["Location on the top line: Phoenix, AZ"])
        self.assertEqual(reasons.what_changed(res), "Your Doc edit: Location on the top line: Phoenix, AZ")
        self.assertEqual(res["reimport"]["doc"], {"version": "9"})
        self.assertEqual(res["attempts"][-1]["kind"], "reimport")
        self.assertGreater(res["files_at"], before.get("files_at", ""))
        self.assertNotEqual(res["files"]["pdf"], before["files"]["pdf"])
        self.assertNotIn("location", [f["name"] for f in res["checks"]["failures"]])
        self.assertEqual(status, "ready")
        self.assertEqual(versions.count(self.jid), 1)
        done = versions.undo(self.conn, self._job())                      # back to Virtual, AZ
        self.assertEqual((done["status"], done["left"]), (self.first, 0))
        res = db.result_of(self._job())
        self.assertIn("Virtual, AZ", docxio.structure(docxio.read(res["files"]["docx"])).contact)
        self.assertTrue(Path(res["files"]["pdf"]).exists())
        with self.assertRaises(ValueError):
            versions.undo(self.conn, self._job())

    def test_after_undo_a_reimport_keeps_the_earlier_contact_edit(self):
        docedit.reimport(self.conn, self._job(), self._doc("Phoenix, AZ"), config.settings(), None)
        docedit.reimport(self.conn, self._job(), self._doc("Tempe, AZ"), config.settings(), None)
        versions.undo(self.conn, self._job())                             # Phoenix is shown again
        docedit.reimport(self.conn, self._job(), self._doc("Phoenix, AZ"), config.settings(), None)
        res = db.result_of(self._job())
        self.assertIn("Phoenix, AZ", docxio.structure(docxio.read(res["files"]["docx"])).contact)
        self.assertEqual(res["reimport"]["changes"], [docedit.NO_TEXT_CHANGE])

    def test_a_failing_edit_is_still_shown_with_what_was_not_kept(self):
        status = docedit.reimport(self.conn, self._job(), self._doc(title="Chief Wizard"), config.settings(), None)
        self.assertEqual(status, "needs_you")
        row = self._job()
        res = db.result_of(row)
        self.assertIn("reimport", res["files"]["pdf"])                    # the new files are the ones shown
        why = reasons.plain(res, row["error"])
        self.assertEqual(why[0]["area"], "Your edit")
        self.assertIn("job title", why[0]["text"])

    def test_a_doc_that_no_longer_fits_the_layout_is_shown_as_the_doc(self):
        doc_pdf = mock.Mock(return_value=self.base.pdf.read_bytes())
        status = docedit.reimport(self.conn, self._job(), self._doc(drop_bullet=True), config.settings(), None,
                                  None, doc_pdf)
        self.assertEqual(status, "needs_you")
        res = db.result_of(self._job())
        doc_pdf.assert_called_once()
        self.assertTrue(res["files"]["pdf"].endswith("reimport1/resume.pdf"))
        self.assertIn("shown as your Doc", self._job()["error"])
        self.assertTrue(server._shown_as_doc(self._job()))                # Fix with note is refused on it
        self.assertNotIn("content", res["files"])

    def test_a_reimport_never_calls_claude(self):
        with mock.patch("resume_tailor.claude_runner.run") as run:
            docedit.reimport(self.conn, self._job(), self._doc(), config.settings(), None)
        run.assert_not_called()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.app = server.App()
        self.httpd = server.make_server(self.app, 0, "127.0.0.1")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.token = config.TOKEN_PATH.read_text().strip()
        self.conn = self.app.conn

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.__exit__(None, None, None)

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def _made(self, status="ready", profile="alex-rivera") -> int:
        jid = db.add_job(self.conn, profile, "Acme", "R", JD_TEXT)
        folder = config.RUNS / str(jid) / "attempt1"
        folder.mkdir(parents=True)
        for name in ("resume.pdf", "resume.docx", "page-1.png"):
            (folder / name).write_bytes(b"v1 " + name.encode())
        files = {"pdf": str(folder / "resume.pdf"), "docx": str(folder / "resume.docx"),
                 "png": str(folder / "page-1.png")}
        db.update(self.conn, jid, result=json.dumps({"files": files, "files_at": "2026-10-12T17:00:00Z",
                                                     "checks": {"failures": []}}))
        db.set_status(self.conn, jid, status, error="x" if status == "needs_you" else None)
        return jid

    def test_cancel_drops_a_ready_or_needs_you_resume_and_restore_brings_it_back(self):
        for profile in ("alex-rivera", "sam-lee"):
            for status in ("ready", "needs_you"):
                with self.subTest(profile=profile, status=status):
                    jid = self._made(status, profile)
                    self.assertEqual(self.call(f"/api/jobs/{jid}/cancel", {})[1]["status"], "cancelled")
                    row = db.get_job(self.conn, jid)
                    self.assertEqual(row["status"], "cancelled")
                    self.assertIsNotNone(row["archived_at"])
                    state = self.call(f"/api/state?profile={profile}")[1]
                    self.assertNotIn(jid, [j["id"] for j in state["jobs"]])
                    gone = next(j for j in state["archived"] if j["id"] == jid)
                    self.assertEqual(gone["dropped_from"], status)
                    listed = next(j for j in self.call(f"/api/jobs?profile={profile}")[1]["jobs"] if j["id"] == jid)
                    self.assertEqual(listed["status"], "cancelled")       # a tracker: no resume for this job now
                    self.assertEqual(self.call("/api/unarchive", {"ids": [jid]})[1]["restored"], [jid])
                    row = db.get_job(self.conn, jid)
                    self.assertEqual((row["status"], row["archived_at"]), (status, None))
                    self.assertNotIn("dropped", db.result_of(row))

    def test_undo_my_edit_after_a_fix_with_note(self):
        jid = self._made("ready")
        self.assertEqual(self.call(f"/api/state?profile=alex-rivera")[1]["jobs"][0]["undo_count"], 0)
        self.assertEqual(self.call(f"/api/jobs/{jid}/fix", {"note": "shorter summary"})[1]["status"], "queued")
        self.assertEqual(versions.count(jid), 1)
        job = db.get_job(self.conn, jid)                        # the worker made a new version, needs you
        folder = config.RUNS / str(jid) / "attempt2"
        folder.mkdir()
        (folder / "resume.pdf").write_bytes(b"v2")
        db.update(self.conn, jid, result=json.dumps({**db.result_of(job), "files": {"pdf": str(folder / "resume.pdf")},
                                                     "files_at": "2026-10-12T18:00:00Z"}))
        db.set_status(self.conn, jid, "needs_you", error="2 pages")
        view = next(j for j in self.call("/api/state?profile=alex-rivera")[1]["jobs"] if j["id"] == jid)
        self.assertEqual((view["undo_count"], view["files_version"]), (1, "2026-10-12T18:00:00Z"))
        self.assertEqual(self.call(f"/api/jobs/{jid}/undo_edit", {"x": 1})[0], 200)
        row = db.get_job(self.conn, jid)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(Path(db.result_of(row)["files"]["pdf"]).read_bytes(), b"v1 resume.pdf")
        self.assertEqual(self.call(f"/api/jobs/{jid}/undo_edit", {})[0], 400)

    def test_approve_after_an_edit_sends_the_edited_pdf_to_the_same_drive_file(self):
        jid = self._made("ready")
        g = mock.Mock()
        g.approve_upload.return_value = {"id": "F1", "webViewLink": "https://drive/F1", "name": "A.pdf"}
        g.update_file.return_value = {"id": "F1", "webViewLink": "https://drive/F1", "name": "A.pdf"}
        with mock.patch.object(self.app, "with_google", side_effect=lambda pid, fn: fn(g)):
            self.assertEqual(self.call(f"/api/jobs/{jid}/approve", {})[1]["drive"]["file_id"], "F1")
            self.assertTrue(self.call(f"/api/jobs/{jid}/approve", {})[1]["already"])
            res = db.result_of(db.get_job(self.conn, jid))
            new_pdf = Path(res["files"]["pdf"]).with_name("edited.pdf")
            new_pdf.write_bytes(b"edited")
            db.update(self.conn, jid, result=json.dumps({**res, "files": {**res["files"], "pdf": str(new_pdf)}}))
            view = next(j for j in self.call("/api/state?profile=alex-rivera")[1]["jobs"] if j["id"] == jid)
            self.assertTrue(view["approved_stale"])
            again = self.call(f"/api/jobs/{jid}/approve", {})[1]
        self.assertTrue(again.get("updated"))
        g.update_file.assert_called_once()
        self.assertEqual(g.update_file.call_args[0][:2], ("F1", b"edited"))
        res = db.result_of(db.get_job(self.conn, jid))
        self.assertEqual(res["approved"]["pdf"], str(new_pdf))
        self.assertFalse(server.approved_stale(res))

    def test_reimport_is_refused_on_an_archived_resume(self):
        jid = self._made("ready")
        db.update(self.conn, jid, gdoc_id="DOC1")
        self.call(f"/api/jobs/{jid}/cancel", {})
        code, data = self.call(f"/api/jobs/{jid}/reimport", {})
        self.assertEqual(code, 400)
        self.assertIn("Restore it first", data["error"])

    def test_undo_back_to_the_approved_content_is_not_stale(self):
        folder = config.RUNS / "9" / "x"
        folder.mkdir(parents=True)
        (folder / "a.pdf").write_bytes(b"same")
        (folder / "b.pdf").write_bytes(b"same")
        (folder / "c.pdf").write_bytes(b"other")
        approved = {"at": "2026-10-12T18:00:00Z", "pdf": str(folder / "a.pdf"), "sha": server.pdf_sha(str(folder / "a.pdf"))}
        self.assertFalse(server.approved_stale({"approved": approved, "files": {"pdf": str(folder / "b.pdf")}}))
        self.assertTrue(server.approved_stale({"approved": approved, "files": {"pdf": str(folder / "c.pdf")}}))

    def test_legacy_approval_is_stale_after_a_later_reimport(self):
        res = {"approved": {"at": "2026-10-12T18:22:29Z"}, "files": {"pdf": "/x.pdf"},
               "attempts": [{"finished": "2026-10-12T18:21:50Z"}, {"kind": "reimport", "finished": "2026-10-12T18:35:10Z"}]}
        self.assertTrue(server.approved_stale(res))
        self.assertFalse(server.approved_stale({**res, "attempts": res["attempts"][:1]}))


if __name__ == "__main__":
    unittest.main()
