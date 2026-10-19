"""pipeline.process with a fake Claude runner: ready / needs_you / limit / login / cancel paths. The PDF engine is
replaced by the base PDF (the content is the base's own, so that is the truthful render) so the tests need no Word."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from resume_tailor import claude_runner, config, db, docedit, docxio, pdfx, pipeline, profiles, render
from tests.helpers import JD_TEXT, TempEnv, add_second_base, example_profile

TOOLS = not pdfx.tools_missing()


def _ok_result(content: dict, model="claude-sonnet-5-5"):
    return claude_runner.RunResult(ok=True, output=content, raw="{}", error=None, kind="ok", limit_until=None,
                                   usage={"output_tokens": 10}, cost_usd=0.01, duration_ms=500, model=model,
                                   num_turns=1, stderr="")


def _bad_result(kind: str, error: str, until=None):
    return claude_runner.RunResult(ok=False, output=None, raw="", error=error, kind=kind, limit_until=until,
                                   usage={}, cost_usd=None, duration_ms=None, model=None, num_turns=None, stderr="")


@unittest.skipUnless(TOOLS, "poppler tools missing")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv({"max_attempts": 2}).__enter__()
        self.conn = db.connect()
        self.prof, self.base = example_profile()
        self.content = docxio.structure(docxio.read(self.base.docx)).as_content()
        self.content.update({"change_notes": ["none"], "match": "test", "review": ["r"], "gaps": [],
                             "added_from_jd": []})
        self.render_patch = mock.patch.object(render, "to_pdf", side_effect=self._fake_pdf)
        self.render_patch.start()

    def tearDown(self):
        self.render_patch.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _fake_pdf(self, docx_bytes, title, work, google=None, docx_path=None, order=("word", "google"), validate=None):
        return Path(self.base.pdf).read_bytes(), "test"

    def _job(self, base_override="ml_engineer"):
        jid = db.add_job(self.conn, "alex-rivera", "Acme", "Machine Learning Engineer", JD_TEXT, None, None, base_override)
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        return db.get_job(self.conn, jid)

    def test_ready_on_first_attempt(self):
        job = self._job()
        runner = mock.Mock(return_value=_ok_result(self.content))
        status = pipeline.process(self.conn, job, config.settings(), None, runner)
        self.assertEqual(status, "ready")
        row = db.get_job(self.conn, job["id"])
        res = db.result_of(row)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(res["base"]["id"], "ml_engineer")
        self.assertIn("your pick", res["base"]["why"])
        self.assertEqual(len(res["attempts"]), 1)
        self.assertTrue(res["checks"]["ok"])
        self.assertEqual(res["model"], "claude-sonnet-5-5")
        run_dir = config.RUNS / str(job["id"]) / "attempt1"
        for name in ("prompt.txt", "claude.json", "content.json", "resume.docx", "resume.pdf", "checks.json",
                     "page-1.png"):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertIn("FACT BANK", (run_dir / "prompt.txt").read_text())
        kinds = [e["kind"] for e in db.events_since(self.conn, 0)]
        self.assertIn("toast", kinds)

    def test_failed_checks_go_to_repair_rounds_then_needs_you(self):
        job = self._job()
        bad = json.loads(json.dumps(self.content))
        bad["jobs"][0]["bullets"][0] = "Spearheaded a robust thing — leveraged synergy across 12,345 systems."
        no_fix = {"bullets": [], "skills": [], "summary": None, "notes": []}
        runner = mock.Mock(side_effect=[_ok_result(bad)] + [_ok_result(no_fix)] * 5)
        status = pipeline.process(self.conn, job, config.settings(), None, runner)
        self.assertEqual(status, "needs_you")
        row = db.get_job(self.conn, job["id"])
        self.assertEqual(row["attempts"], 6)                     # 1 full + 5 repair rounds (max_repairs default)
        self.assertIn("banned", row["error"].lower())
        second_prompt = runner.call_args_list[1][0][0]
        self.assertIn("ITEMS TO FIX", second_prompt)
        self.assertIn("banned", second_prompt.lower())
        self.assertTrue((config.RUNS / str(job["id"]) / "attempt2" / "checks.json").exists())

    def test_layout_mismatch_counts_as_failed_attempt(self):
        job = self._job()
        bad = json.loads(json.dumps(self.content))
        bad["jobs"][0]["bullets"].pop()
        runner = mock.Mock(return_value=_ok_result(bad))
        self.assertEqual(pipeline.process(self.conn, job, config.settings(), None, runner), "needs_you")
        self.assertIn("fit the base layout", db.get_job(self.conn, job["id"])["error"])

    def test_usage_limit_raises_limit_and_keeps_attempt_record(self):
        job = self._job()
        until = datetime.now(timezone.utc) + timedelta(minutes=5)
        runner = mock.Mock(return_value=_bad_result("limit", "usage limit reached", until))
        with self.assertRaises(pipeline.Limit) as ctx:
            pipeline.process(self.conn, job, config.settings(), None, runner)
        self.assertEqual(ctx.exception.until, until)
        res = db.result_of(db.get_job(self.conn, job["id"]))
        self.assertIn("usage limit", res["attempts"][0]["failures"][0])

    def test_missing_login_raises_login(self):
        job = self._job()
        runner = mock.Mock(return_value=_bad_result("login", "not logged in"))
        with self.assertRaises(pipeline.Login):
            pipeline.process(self.conn, job, config.settings(), None, runner)

    def test_cancel_is_honoured_between_steps(self):
        job = self._job()
        db.cancel(self.conn, job["id"])
        runner = mock.Mock(return_value=_ok_result(self.content))
        with self.assertRaises(pipeline.Cancelled):
            pipeline.process(self.conn, job, config.settings(), None, runner)
        runner.assert_not_called()

    def test_choose_base_prefers_override_then_score(self):
        add_second_base()
        prof = profiles.load("alex-rivera")
        scores = {"best_id": "data_scientist", "vendor_label": "Workday",
                  "scores": [{"id": "ml_engineer", "label": "ML", "pct": 50},
                             {"id": "data_scientist", "label": "DS", "pct": 80}]}
        base, why = pipeline.choose_base(prof, {"base_override": None}, scores)
        self.assertEqual(base.id, "data_scientist")
        self.assertIn("DS 80%", why)
        base, why = pipeline.choose_base(prof, {"base_override": "ml_engineer"}, scores)
        self.assertEqual((base.id, why), ("ml_engineer", "your pick (50% match)"))
        base, why = pipeline.choose_base(prof, {"base_override": None}, None)
        self.assertEqual(base.id, prof.bases[0].id)

    def test_reimport_refills_base_and_reruns_checks(self):
        job = self._job()
        runner = mock.Mock(return_value=_ok_result(self.content))
        pipeline.process(self.conn, job, config.settings(), None, runner)
        edited = docxio.fill(docxio.read(self.base.docx), self.content)       # "the Google Doc export"
        before = db.result_of(db.get_job(self.conn, job["id"]))
        self.assertTrue(all(before.get(k) is not None for k in pipeline.NOTE_KEYS), before)
        status = docedit.reimport(self.conn, db.get_job(self.conn, job["id"]), edited, config.settings(), None)
        self.assertEqual(status, "ready")
        res = db.result_of(db.get_job(self.conn, job["id"]))
        self.assertEqual(res["attempts"][-1]["kind"], "reimport")
        self.assertTrue((config.RUNS / str(job["id"]) / "reimport1" / "resume.pdf").exists())
        for key in pipeline.NOTE_KEYS:                    # the edited Doc has no notes; Claude's stay
            self.assertEqual(res[key], before[key], key)

    def test_engine_order(self):
        self.assertEqual(pipeline.engine_order({"pdf_engine": "google"}), ("google",))
        self.assertEqual(pipeline.engine_order({"pdf_engine": "word"}), ("word", "google"))
        self.assertEqual(pipeline.engine_order({"pdf_engine": "libreoffice"}), ("libreoffice",))
        with mock.patch.object(render, "libreoffice_available", return_value=True), \
                mock.patch.object(render, "word_available", return_value=False):
            self.assertEqual(pipeline.engine_order({}), ("libreoffice", "google"))
        with mock.patch.object(render, "libreoffice_available", return_value=False), \
                mock.patch.object(render, "word_available", return_value=False):
            self.assertEqual(pipeline.engine_order({"pdf_engine": "auto"}), ("google",))


if __name__ == "__main__":
    unittest.main()
