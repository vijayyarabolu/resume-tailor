"""repair mode. Mechanical fixes (hyphen table, unknown-skill drop), the repair plan built from check
failures, applying Claude's replacements, and the pipeline running full -> repair / mechanical rounds with a fake
runner (the PDF engine is replaced by the base PDF, as in test_pipeline)."""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from resume_tailor import checks, claude_runner, config, db, docxio, pdfx, pipeline, render, repair, rules
from tests.helpers import JD_TEXT, TempEnv, example_profile

TOOLS = not pdfx.tools_missing()
PRULES = rules.for_profile("alex-rivera")


def _content():
    return {"headline": "AI Engineer", "summary": "Four sentences of summary text here. Second one. Third. Fourth.",
            "skills": [{"category": "Cloud", "items": ["AWS", "GCP", "Docker"]},
                       {"category": "ML", "items": ["PyTorch", "Optimization"]}],
            "jobs": [{"company": "Acme, USA", "bullets": [
                "Deployed models on GCP and AWS with Docker for real-time image-based defect detection at scale "
                "for the manufacturing customers of the platform team.",
                "Built pipelines with Airflow and PySpark, cutting runtime from 40 to 10 minutes for the team."]},
                     {"company": "Old Co", "bullets": ["Led model optimization work across the platform daily."]}]}


class HyphenTableTests(unittest.TestCase):
    def test_table_and_suffix_forms(self):
        self.assertEqual(repair.unhyphenate("real-time image-based detection"), "real time image based detection")
        self.assertEqual(repair.unhyphenate("GPU-accelerated cloud-based end-to-end runs"),
                         "GPU accelerated cloud based end to end runs")
        self.assertEqual(repair.unhyphenate("pre-trained BERT"), "pretrained BERT")
        self.assertEqual(repair.unhyphenate("no compounds here"), "no compounds here")

    def test_fix_protruding_rewrites_only_the_element_on_that_line(self):
        content = _content()
        fixed = repair.fix_protruding(content, ["AWS with Docker for real-time image-based defect detection at"], PRULES)
        self.assertTrue(fixed.changed)
        self.assertIn("real time image based", fixed.content["jobs"][0]["bullets"][0])
        self.assertEqual(fixed.content["jobs"][0]["bullets"][1], content["jobs"][0]["bullets"][1])
        self.assertIn("real-time", content["jobs"][0]["bullets"][0])        # the input was not mutated

    def test_fix_protruding_respects_the_word_limit(self):
        content = _content()
        long = " ".join(["word"] * (PRULES["bullet_words"][1] - 2)) + " real-time run."
        content["jobs"][0]["bullets"][0] = long
        fixed = repair.fix_protruding(content, [long[-50:]], PRULES)
        self.assertFalse(fixed.changed)


class UnknownSkillDropTests(unittest.TestCase):
    def test_product_names_leave_skills_and_prose(self):
        content = _content()
        out = repair.drop_unknown_terms(content, ["GCP"], PRULES)
        self.assertEqual(out.content["skills"][0]["items"], ["AWS", "Docker"])
        self.assertEqual(out.content["jobs"][0]["bullets"][0],
                         "Deployed models on AWS with Docker for real-time image-based defect detection at scale "
                         "for the manufacturing customers of the platform team.")
        self.assertTrue(any("skills 'Cloud'" in n for n in out.notes))
        self.assertIn("GCP", content["skills"][0]["items"])                 # immutability

    def test_generic_words_leave_skills_only(self):
        out = repair.drop_unknown_terms(_content(), ["Optimization"], PRULES, {"Optimization": "Data Science"})
        self.assertEqual(out.content["skills"][1]["items"], ["PyTorch"])
        self.assertIn("optimization", out.content["jobs"][1]["bullets"][0])

    def test_lexicon_names_match_their_written_forms(self):
        self.assertEqual(repair.term_variants("Infrastructure as Code (IaC)"),
                         ["Infrastructure as Code (IaC)", "Infrastructure as Code", "IaC"])
        content = _content()
        content["summary"] = "Work is infra, SRE, IaC and agent observability across the fleet."
        out = repair.drop_unknown_terms(content, ["Infrastructure as Code (IaC)"], PRULES, {})
        self.assertEqual(out.content["summary"], "Work is infra, SRE and agent observability across the fleet.")
        report = _report([(5, "unknown_skills", "not in the base resumes or known skills: 'Infrastructure as Code (IaC)'")])
        prof, base = example_profile()
        plan = repair.plan(report, content, pipeline.measure_base(base), PRULES, set())
        self.assertIn(("summary", 0, 0), {i.key for i in plan.items})

    def test_term_hiding_in_a_category_name_is_dropped(self):
        content = _content()
        content["skills"][0]["category"] = "Cloud & IaC"
        out = repair.drop_unknown_terms(content, ["Infrastructure as Code (IaC)"], PRULES, {})
        self.assertEqual(out.content["skills"][0]["category"], "Cloud")
        self.assertEqual(repair._clean_category("IaC & Cloud", "IaC"), "Cloud")
        self.assertEqual(repair._clean_category("Cloud, IaC and Data", "IaC"), "Cloud and Data")
        self.assertEqual(repair._clean_category("MLOps", "IaC"), "MLOps")

    def test_remove_term_keeps_sentences_tidy(self):
        self.assertEqual(repair.remove_term("Served APIs on GCP, AWS and Azure.", "GCP"), "Served APIs on AWS and Azure.")
        self.assertEqual(repair.remove_term("Used Terraform (IaC) daily.", "IaC"), "Used Terraform daily.")
        self.assertEqual(repair.remove_term("Ran vLLM and TGI servers.", "vLLM"), "Ran TGI servers.")


def _report(failures, info=None):
    return checks.Report([checks.Failure(c, n, m) for c, n, m in failures], info or {})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.prof, self.base = example_profile()
        self.measure = pipeline.measure_base(self.base)

    def test_items_follow_the_failures(self):
        content = _content()
        report = _report([
            (7, "bullet_words", "bullet 2 of role 1 (Acme, USA) has 26 words; the rule is 15 to 25"),
            (4, "widow", "bullet 2 of role 1 (Acme, USA) is a widow: its last line fills 10% of the width"),
            (7, "summary_words", "the summary has 12 words; the rule is 50 to 80"),
            (4, "skill_lines", "skills line 'ML' wraps to 3 lines (at most 2); drop a term from it"),
            (6, "ats_score", "ATS score 60% is below the base's 70%; cover the missing JD keywords: Airflow, gRPC"),
            (3, "format", "word/styles.xml differs from the base"),
        ], {"ats": {"missing": ["Airflow", "gRPC"], "uncovered_known": []}})
        plan = repair.plan(report, content, self.measure, PRULES, {"Airflow", "Python"})
        kinds = {(i.kind, i.job, i.index) for i in plan.items}
        self.assertIn(("bullet", 1, 2), kinds)
        self.assertIn(("summary", 0, 0), kinds)
        self.assertIn(("skills", 0, 2), kinds)
        bullet = next(i for i in plan.items if i.key == ("bullet", 1, 2))
        self.assertEqual(len(bullet.rules), 2)                 # both failures of that bullet, one item
        self.assertEqual(plan.keywords, ("Airflow",))          # gRPC is not owned: never added
        self.assertEqual(plan.unrepairable, ("word/styles.xml differs from the base",))
        self.assertFalse(plan.empty)

    def test_a_keyword_the_base_matched_is_owned(self):
        # the base matched the ATS's literal "APIs" while the tailored resume said "API"
        report = _report([(6, "ats_score", "ATS score 83% is below the base's 100%; cover the missing JD keywords: APIs")],
                         {"ats": {"missing": ["APIs", "gRPC"], "base_missing": ["gRPC"], "uncovered_known": []}})
        plan = repair.plan(report, _content(), self.measure, PRULES, {"API"})
        self.assertEqual(plan.keywords, ("APIs",))             # gRPC: the base lacked it too, still never added

    def test_too_long_page_targets_longest_elements(self):
        content = _content()
        report = _report([(1, "pages", "the PDF has 2 pages; it must be exactly 1")],
                         {"overflow_lines": 2, "fit": {"bullet_lines": {"1.1": 3, "1.2": 2, "2.1": 2},
                                                       "skill_lines": {}}})
        plan = repair.plan(report, content, self.measure, PRULES, set())
        self.assertEqual(plan.shorten_lines, 2)
        self.assertEqual(plan.items[0].key, ("bullet", 1, 1))   # the 3-line bullet first
        self.assertIn(("summary", 0, 0), {i.key for i in plan.items})   # the summary can always lose a line
        self.assertGreaterEqual(len(plan.items), 3)
        self.assertIn("2 line(s) too long", plan.items[0].rules[0])

    def test_longer_summary_is_the_first_target(self):
        content = _content()
        report = _report([(1, "pages", "the PDF has 2 pages; it must be exactly 1")],
                         {"overflow_lines": 1, "fit": {"bullet_lines": {}, "skill_lines": {}, "summary_lines": 5}})
        plan = repair.plan(report, content, self.measure, PRULES, set())
        self.assertEqual(plan.items[0].kind, "summary")
        self.assertIn("renders 5 lines", plan.items[0].rules[0])

    def test_apply_changes_only_planned_items(self):
        content = _content()
        report = _report([(7, "bullet_words", "bullet 1 of role 1 (Acme, USA) has 26 words; the rule is 15 to 25"),
                          (4, "skill_lines", "skills line 'Cloud' wraps to 3 lines (at most 2); drop a term from it")])
        plan = repair.plan(report, content, self.measure, PRULES, set())
        output = {"bullets": [{"job": 1, "index": 1, "text": "New bullet one."},
                              {"job": 1, "index": 2, "text": "Sneaky change."},
                              {"job": 9, "index": 1, "text": "Nowhere."}],
                  "skills": [{"index": 1, "category": "Cloud", "items": ["AWS"]},
                             {"index": 2, "category": "ML", "items": ["Nope"]}],
                  "summary": "A new summary.", "notes": []}
        new, notes = repair.apply(content, output, plan)
        self.assertEqual(new["jobs"][0]["bullets"][0], "New bullet one.")
        self.assertEqual(new["jobs"][0]["bullets"][1], content["jobs"][0]["bullets"][1])
        self.assertEqual(new["skills"][0]["items"], ["AWS"])
        self.assertEqual(new["skills"][1]["items"], ["PyTorch", "Optimization"])
        self.assertEqual(new["summary"], content["summary"])
        self.assertEqual(content["jobs"][0]["bullets"][0][:8], "Deployed")   # input untouched
        self.assertTrue(any("not up for repair" in n for n in notes))

    def test_ats_keywords_allow_two_free_bullets(self):
        content = _content()
        plan = repair.Plan(items=(), keywords=("Airflow",))
        output = {"bullets": [{"job": 1, "index": 1, "text": "A."}, {"job": 1, "index": 2, "text": "B."},
                              {"job": 2, "index": 1, "text": "C."}], "skills": [], "summary": None, "notes": []}
        new, notes = repair.apply(content, output, plan)
        self.assertEqual([j["bullets"] for j in new["jobs"]], [["A.", "B."], [content["jobs"][1]["bullets"][0]]])


def _ok(output, tokens=10, cost=0.01):
    return claude_runner.RunResult(ok=True, output=output, raw="{}", error=None, kind="ok", limit_until=None,
                                   usage={"output_tokens": tokens}, cost_usd=cost, duration_ms=500,
                                   model="claude-sonnet-5-5", num_turns=1, stderr="")


@unittest.skipUnless(TOOLS, "poppler tools missing")
class PipelineRepairTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv({"max_attempts": 2, "max_repairs": 2}).__enter__()
        self.conn = db.connect()
        self.prof, self.base = example_profile()
        self.content = docxio.structure(docxio.read(self.base.docx)).as_content()
        self.content.update({"change_notes": [], "match": "m", "review": [], "gaps": [], "added_from_jd": []})
        self.render_patch = mock.patch.object(render, "to_pdf", side_effect=self._fake_pdf)
        self.render_patch.start()

    def tearDown(self):
        self.render_patch.stop()
        self.conn.close()
        self.env.__exit__(None, None, None)

    def _fake_pdf(self, docx_bytes, title, work, google=None, docx_path=None, order=("word", "google"), validate=None):
        return Path(self.base.pdf).read_bytes(), "test"

    def _job(self):
        jid = db.add_job(self.conn, "alex-rivera", "Acme", "Machine Learning Engineer", JD_TEXT, None, None, "ml_engineer")
        db.claim(self.conn, jid, str(config.RUNS / str(jid)))
        return db.get_job(self.conn, jid)

    def test_repair_round_sends_only_the_failing_bullet_and_fixes_it(self):
        job = self._job()
        bad = copy.deepcopy(self.content)
        good_bullet = bad["jobs"][0]["bullets"][0]
        bad["jobs"][0]["bullets"][0] = " ".join(["word"] * 26) + "."
        fix = {"bullets": [{"job": 1, "index": 1, "text": good_bullet}], "skills": [], "summary": None,
               "notes": ["restored"]}
        runner = mock.Mock(side_effect=[_ok(bad, tokens=20000, cost=0.9), _ok(fix, tokens=800, cost=0.05)])
        status = pipeline.process(self.conn, job, config.settings(), None, runner)
        self.assertEqual(status, "ready")
        row = db.get_job(self.conn, job["id"])
        res = db.result_of(row)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual([a["kind"] for a in res["attempts"]], ["full", "repair"])
        self.assertLess(res["attempts"][1]["cost_usd"], res["attempts"][0]["cost_usd"])
        prompt2 = runner.call_args_list[1][0][0]
        self.assertIn("ITEMS TO FIX", prompt2)
        self.assertIn("bullet 1 of role 1", prompt2)
        self.assertNotIn("PREVIOUS ATTEMPT FAILED", prompt2)
        self.assertEqual(runner.call_args_list[1][0][1], __import__("resume_tailor.prompts", fromlist=["x"]).REPAIR_SCHEMA)
        self.assertTrue((config.RUNS / str(job["id"]) / "attempt2" / "plan.json").exists())
        self.assertTrue(res["checks"]["ok"])

    def test_unknown_skill_is_dropped_without_a_claude_call(self):
        job = self._job()
        bad = copy.deepcopy(self.content)
        bad["skills"][0]["items"] = ["Kubeflow"] + list(bad["skills"][0]["items"])
        bad["jobs"][0]["bullets"][0] = bad["jobs"][0]["bullets"][0].rstrip(".") + " with Kubeflow."
        runner = mock.Mock(return_value=_ok(bad))
        status = pipeline.process(self.conn, job, config.settings(), None, runner)
        self.assertEqual(status, "ready")
        res = db.result_of(db.get_job(self.conn, job["id"]))
        self.assertEqual([a["kind"] for a in res["attempts"]], ["full", "mechanical"])
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(any("dropped 'Kubeflow'" in n for n in res["attempts"][1]["notes"]))
        self.assertNotIn("Kubeflow", json.dumps(res["diff"]))

    def test_repairs_stop_at_max_repairs_with_the_last_failures(self):
        job = self._job()
        bad = copy.deepcopy(self.content)
        bad["jobs"][0]["bullets"][0] = " ".join(["word"] * 34) + "."
        no_fix = {"bullets": [], "skills": [], "summary": None, "notes": []}
        runner = mock.Mock(side_effect=[_ok(bad), _ok(no_fix), _ok(no_fix)])
        self.assertEqual(pipeline.process(self.conn, job, config.settings(), None, runner), "needs_you")
        row = db.get_job(self.conn, job["id"])
        self.assertEqual(runner.call_count, 3)                   # 1 full + 2 repairs, never a second full attempt
        self.assertIn("34 words", row["error"])
        self.assertIn("2 repair rounds", row["error"])

    def test_tailored_scores_carry_the_base_families(self):
        entries = pipeline._resume_entries(self.prof, "text", families=("ml",))
        self.assertEqual(entries[-1]["families"], ["ml"])
        self.assertEqual(pipeline.engine_order({"pdf_engine": "google"}), ("google",))
        self.assertEqual(pipeline.engine_order({"pdf_engine": "google", "word_fallback": True}), ("google", "word"))


if __name__ == "__main__":
    unittest.main()


class DropLeavesNoBareParentheses(unittest.TestCase):
    """Dropping 'NoSQL' from 'NoSQL (Redis, Neo4j)' must leave 'Redis', 'Neo4j', not '(Redis, Neo4j)',
    and the term leaves added_from_jd too."""

    def test_parenthetical_items_are_unwrapped_and_added_from_jd_follows(self):
        self.assertEqual(repair.remove_from_items(["OpenSearch", "NoSQL (Redis, Neo4j)"], "NoSQL"),
                         ["OpenSearch", "Redis", "Neo4j"])
        content = {"skills": [{"category": "Vector", "items": ["NoSQL (Redis, Neo4j)"]}], "jobs": [], "summary": "",
                   "added_from_jd": ["NoSQL (label for Redis and Neo4j)", "Git"]}
        out = repair.drop_unknown_terms(content, ["NoSQL"], {})
        self.assertEqual(out.content["skills"][0]["items"], ["Redis", "Neo4j"])
        self.assertEqual(out.content["added_from_jd"], ["Git"])
        self.assertEqual(content["added_from_jd"][0], "NoSQL (label for Redis and Neo4j)", "input not mutated")
