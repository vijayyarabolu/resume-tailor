"""Fix with note is ONE Claude call that keeps what the note asked (a skill the note names passes the
honesty check and is saved to the person's confirmed list), lists the problems instead of repairing, and every
Claude call (full, repair, note) uses the same model and effort for both people."""
import json
import types
import unittest
from pathlib import Path
from unittest import mock

from resume_tailor import checks, claude_runner, confirmed, config, db, docxio, pdfx, pipeline, profiles, prompts, \
    reasons, render, versions
from tests.helpers import JD_TEXT, TempEnv
from tests.test_pipeline import _ok_result

TOOLS = not pdfx.tools_missing()
NOTE_108 = "I know data warehousing make it fit naturally where ever that fits naturally"
DW = "Data Warehousing"
NO_FIX = {"bullets": [], "skills": [], "summary": None, "notes": []}
PEOPLE = {"alex-rivera": "ml_engineer", "sam-lee": "ml_engineer"}


def _note_out(**kw) -> dict:
    return {"bullets": [], "skills": [], "summary": None, "notes": [], "location": None, "cannot": None, **kw}


class ConfirmedListTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_the_note_names_a_term_written_out_or_by_its_stems(self):
        self.assertTrue(confirmed.named_in_note(DW, NOTE_108))
        self.assertTrue(confirmed.named_in_note(DW, "add data warehouse experience"))
        self.assertTrue(confirmed.named_in_note("dbt", "I use dbt daily"))
        self.assertFalse(confirmed.named_in_note("Snowflake", NOTE_108))
        self.assertFalse(confirmed.named_in_note(DW, "make the summary shorter"))

    def test_no_false_confirmations(self):
        self.assertFalse(confirmed.named_in_note("R", "make the summary shorter"))          # inside a word
        self.assertFalse(confirmed.named_in_note("Go", "go with the shorter wording"))      # short: own case only
        self.assertFalse(confirmed.named_in_note("Kafka", "I have never used Kafka, take it out"))
        self.assertFalse(confirmed.named_in_note("Kafka", "remove Kafka from skills"))
        self.assertFalse(confirmed.named_in_note("Go", "Go ahead and shorten the summary"))  # sentence start
        self.assertTrue(confirmed.named_in_note("R", "I know R and SAS"))
        self.assertTrue(confirmed.named_in_note("Go", "I write Go at work"))
        self.assertTrue(confirmed.named_in_note("Kafka", "I used Kafka at Northwind Analytics, add it"))

    def test_only_a_stated_number_newly_used_becomes_a_fact(self):
        before = {"summary": "Cut costs.", "jobs": [{"bullets": ["Built 3 things"]}]}
        after = {"summary": "Cut costs 30%.", "jobs": [{"bullets": ["Built 3 things"]}]}
        self.assertTrue(pipeline._new_fact("I cut costs 30% at Northwind Analytics", before, after))
        self.assertFalse(pipeline._new_fact("make bullet 1 of the last role a bit shorter", before, after))
        self.assertFalse(pipeline._new_fact("keep bullet 3 as it was", before, after))
        self.assertFalse(pipeline._new_fact("I cut costs 30%", after, after))                # not new in the resume

    def test_saved_once_per_person_with_where_it_came_from(self):
        self.assertEqual(confirmed.add("alex-rivera", "skills", [DW, "data warehousing"], 108), [DW])
        self.assertEqual(confirmed.add("alex-rivera", "skills", [DW], 109), [])
        line = confirmed.path("alex-rivera", "skills").read_text().strip()
        self.assertRegex(line, r"^Data Warehousing  # \[from note, job 108, \d{4}-\d{2}-\d{2}\]$")
        self.assertEqual(confirmed.read("alex-rivera", "skills"), [DW])
        self.assertEqual(confirmed.read("sam-lee", "skills"), [])            # per person
        with self.assertRaises(ValueError):
            confirmed.path("../x", "skills")

    def test_the_profile_carries_what_the_person_confirmed(self):
        before = profiles.load("alex-rivera")
        self.assertNotIn(DW, before.known_skills)
        confirmed.add("alex-rivera", "skills", [DW], 108)
        confirmed.add("alex-rivera", "facts", ["cut report time 37% at Northwind Analytics"], 108)
        after = profiles.load("alex-rivera")
        self.assertIn(DW, after.known_skills)
        self.assertIn("cut report time 37% at Northwind Analytics", after.skill_text)
        self.assertNotIn(DW, profiles.load("sam-lee").known_skills)
        self.assertNotIn(DW, before.known_skills)                          # a new Profile, the cached one untouched


class HonestyWithNoteTests(unittest.TestCase):
    def _inp(self, notes=()):
        struct = types.SimpleNamespace(summary="Built a Data Warehousing layer cutting cost 37%.", jobs=[])
        inp = types.SimpleNamespace(pid="alex-rivera", profile_rules={}, base_structure=types.SimpleNamespace(headline="X"),
                                    role="", location="", known_skills=[], all_base_texts=["base 10"],
                                    skill_text="", notes=notes)
        scores = {"found": {"tailored": {DW: 1}, "known": {}}, "jd_found": {}, "categories": {}}
        return checks.check_honesty(inp, struct, scores)

    def test_a_term_and_a_number_the_note_states_pass_check_5(self):
        fails, info = self._inp()
        self.assertEqual({f.name for f in fails}, {"unknown_skills", "numbers"})
        fails, info = self._inp((NOTE_108 + "; it cut cost 37%",))
        self.assertEqual(fails, [])
        self.assertEqual(info["confirmed_by_note"], [DW])


@unittest.skipUnless(TOOLS, "poppler tools missing")
class NoteRoundTests(unittest.TestCase):
    """Real-shaped notes for both people: the change lands, one Claude call, nothing runs after it."""

    def setUp(self):
        self.env = TempEnv({"max_attempts": 1, "max_repairs": 1, "max_claude_calls": 2,
                            "model": "claude-sonnet-5-5", "effort": "medium"}).__enter__()
        self.conn = db.connect()
        self.patch = mock.patch.object(render, "to_pdf", side_effect=self._pdf)
        self.patch.start()
        self.base_pdf = None

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _pdf(self, *a, **k):
        return Path(self.base_pdf).read_bytes(), "test"

    def _start(self, pid: str, content: dict | None = None) -> tuple:
        prof = profiles.load(pid)
        base = prof.base(PEOPLE[pid])
        self.base_pdf = base.pdf
        base_content = base.structure.as_content()
        content = content or json.loads(json.dumps(base_content))
        content.update({"change_notes": [], "match": "m", "review": [], "gaps": [], "added_from_jd": []})
        jid = db.add_job(self.conn, pid, "Acme", "Machine Learning Engineer", JD_TEXT, base_override=base.id)
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        runner = mock.Mock(side_effect=[_ok_result(content), _ok_result(NO_FIX)])
        pipeline.process(self.conn, db.get_job(self.conn, jid), config.settings(), None, runner)
        db.set_status(self.conn, jid, "needs_you", error="x")
        return jid, base_content

    def _fix(self, jid: int, note: str, output: dict) -> tuple:
        db.fix_with_note(self.conn, jid, note)
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        runner = mock.Mock(return_value=_ok_result(output))
        status = pipeline.process(self.conn, db.get_job(self.conn, jid), config.settings(), None, runner)
        row = db.get_job(self.conn, jid)
        res = db.result_of(row)
        path = (res["attempts"][-1].get("files") or {}).get("content")    # none: the note changed nothing
        return status, runner, res, json.loads(Path(path).read_text()) if path else None

    def test_a_note_naming_a_skill_keeps_it_in_one_call_and_saves_it(self):
        for pid in PEOPLE:
            with self.subTest(pid):
                jid, base = self._start(pid)                 # the note adds the skill to Skills and a bullet
                self.assertNotIn(DW, profiles.load(pid).known_skills)
                line = base["skills"][2]
                last_job = len(base["jobs"])
                bullet = base["jobs"][-1]["bullets"][2].rstrip(".") + " and data warehousing."
                status, runner, res, last = self._fix(jid, NOTE_108, _note_out(
                    bullets=[{"job": last_job, "index": 3, "text": bullet}], notes=["added DW"],
                    skills=[{"index": 3, "category": line["category"], "items": line["items"] + [DW]}]))
                self.assertEqual(runner.call_count, 1)                   # ONE call, though 2 were used already
                self.assertEqual(res["attempts"][-1]["kind"], "fix")    # no repair / mechanical round after it
                self.assertIn(DW, last["skills"][2]["items"])            # still there: nothing took it out
                self.assertIn("data warehousing", last["jobs"][-1]["bullets"][2])
                self.assertNotIn("unknown_skills", [f["name"] for f in res["checks"]["failures"]])
                self.assertEqual(res["fix"]["confirmed"], [DW])               # not on any list before the note
                self.assertIn(DW, confirmed.read(pid, "skills"))
                self.assertIn(DW, profiles.load(pid).known_skills)
                self.assertNotIn(DW, res["checks"]["info"]["honesty"]["unknown_terms"])
                self.assertIn("bullet 3", " · ".join(res["fix"]["changes"]))
                self.assertIn(f"Added {DW} to Skills", " · ".join(res["fix"]["changes"]))
                self.assertTrue(reasons.what_changed(res).startswith(f"Your note: Added {DW} to Skills"))
                prompt = runner.call_args[0][0]
                self.assertIn("WHAT THE NOTE SAYS IS CONFIRMED", prompt)
                self.assertEqual(runner.call_args[0][1], prompts.NOTE_SCHEMA)
                if status == "needs_you":                               # the test PDF is the base's: listed as is
                    self.assertTrue(db.get_job(self.conn, jid)["error"])

    def test_reword_shorten_and_keep_as_in_base_for_both_people(self):
        for pid in PEOPLE:
            jid, base = self._start(pid)
            words = base["summary"].split()
            cases = [
                ("reword bullet 1 of the first job", _note_out(bullets=[{"job": 1, "index": 1, "text":
                    "Delivered " + " ".join(base["jobs"][0]["bullets"][0].split()[1:])}]), "bullet 1"),
                ("only make the summary shorter", _note_out(summary=" ".join(words[:-6]) + "."), "Summary shortened"),
                ("keep bullet 1 of the first job as in base", _note_out(bullets=[{"job": 1, "index": 1, "text":
                    base["jobs"][0]["bullets"][0]}]), "bullet 1"),
            ]
            for note, out, said in cases:
                with self.subTest(pid=pid, note=note):
                    db.set_status(self.conn, jid, "needs_you", error="x")
                    status, runner, res, last = self._fix(jid, note, out)
                    self.assertEqual(runner.call_count, 1)
                    self.assertTrue(res["fix"]["applied"])
                    self.assertIn(said, " · ".join(res["fix"]["changes"]))
                    self.assertEqual(res["attempts"][-1]["kind"], "fix")
            self.assertEqual(last["jobs"][0]["bullets"][0], base["jobs"][0]["bullets"][0])

    def test_a_note_that_cannot_be_done_says_so(self):
        jid, _ = self._start("alex-rivera")
        status, runner, res, last = self._fix(jid, "make my title Staff Engineer", _note_out(
            cannot="the title under the name is fixed to your base's title"))
        self.assertEqual(status, "needs_you")
        self.assertFalse(res["fix"]["applied"])
        why = reasons.plain(res, db.get_job(self.conn, jid)["error"])
        self.assertEqual(why[0]["area"], "Your note")
        self.assertIn("the title under the name is fixed", " ".join(x["text"] for x in why))
        self.assertIn("changed nothing", db.get_job(self.conn, jid)["error"])

    def test_a_note_not_applied_keeps_a_ready_resume_ready_and_offers_no_undo(self):
        jid, _ = self._start("alex-rivera")
        db.set_status(self.conn, jid, "ready")
        versions.snapshot(db.get_job(self.conn, jid), "fix", "make my title Staff Engineer")   # as the server does
        status, runner, res, last = self._fix(jid, "make my title Staff Engineer", _note_out(cannot="title is fixed"))
        self.assertEqual(status, "ready")
        self.assertEqual(db.get_job(self.conn, jid)["status"], "ready")
        self.assertEqual(versions.count(jid), 0)
        self.assertIn("not applied", reasons.what_changed(res))

    def test_after_undo_the_next_note_writes_a_new_folder(self):
        jid, base = self._start("alex-rivera")
        words = base["summary"].split()
        versions.snapshot(db.get_job(self.conn, jid), "fix", "shorter summary")
        _, _, res1, _ = self._fix(jid, "shorter summary", _note_out(summary=" ".join(words[:-5]) + "."))
        first = res1["files"]["pdf"]
        first_bytes = Path(first).read_bytes()
        versions.undo(self.conn, db.get_job(self.conn, jid))
        _, _, res2, _ = self._fix(jid, "even shorter summary", _note_out(summary=" ".join(words[:-9]) + "."))
        self.assertNotEqual(res2["files"]["pdf"], first)
        self.assertEqual(Path(first).read_bytes(), first_bytes)            # the undone version's files untouched

    def test_the_note_can_move_the_contact_location(self):
        jid, _ = self._start("alex-rivera")
        status, runner, res, last = self._fix(jid, "put Phoenix, AZ beside my email", _note_out(location="Phoenix, AZ"))
        self.assertEqual(last["user_location"], "Phoenix, AZ")
        self.assertIn("Location on the top line: Phoenix, AZ", res["fix"]["changes"])
        docx = docxio.structure(docxio.read(res["files"]["docx"]))
        self.assertIn("Phoenix, AZ", docx.contact)


class SameSettingsTests(unittest.TestCase):
    """: claude-sonnet-5-5 with --effort medium on every call kind, for both people."""

    def test_the_defaults_are_sonnet_5_5_and_three_calls(self):
        d = config.DEFAULTS
        self.assertEqual((d["model"], d["max_claude_calls"], d["claude_backend"]), ("claude-sonnet-5-5", 3, "api"))

    def test_every_call_kind_passes_model_and_effort_for_both_people(self):
        settings = {"model": "claude-sonnet-5-5", "effort": "medium"}
        for pid in PEOPLE:
            prof = profiles.load(pid)
            ctx = types.SimpleNamespace(settings=settings, prof=prof)
            for kind in ("full", "repair", "fix"):
                runner = mock.Mock(return_value=_ok_result({}))
                with TempEnv() as env:
                    pipeline._call(ctx, None, runner, f"{kind} prompt", {}, env.tmp)
                kw = runner.call_args.kwargs
                self.assertEqual((kw["model"], kw["effort"], kw["config_dir"]),
                                 ("claude-sonnet-5-5", "medium", prof.config_dir), (pid, kind))

    def test_the_command_line_carries_model_and_effort(self):
        fake = mock.Mock(stdout=json.dumps({"type": "result", "subtype": "success", "result": "{}",
                                            "structured_output": {}}), stderr="", returncode=0)
        with mock.patch("subprocess.run", return_value=fake) as run:
            claude_runner.run("p", {}, model="claude-sonnet-5-5", effort="medium", config_dir="~/.claude-sam")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")


if __name__ == "__main__":
    unittest.main()
