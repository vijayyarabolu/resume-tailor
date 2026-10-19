"""With `jd_skills_allowed = true` in rules.toml the resume mirrors the JD's hard skills: a JD skill is never
treated as unknown or dropped, every required JD skill must be on the resume, the prompt says so, and repairs may
add JD skills. With it off (the example's default) only skills the person lists count."""
import types
import unittest

from resume_tailor import checks, config, profiles, prompts, repair, rules
from tests.helpers import TempEnv, example_profile

SCORES = {"terms": [{"name": "Snowflake", "need": True}, {"name": "Rust", "need": True},
                    {"name": "dbt", "need": False}],
          "jd_found": {"Snowflake": 1, "Rust": 1, "dbt": 1}, "found": {"known": {}, "tailored": {"Snowflake": 1}},
          "scores": [{"id": "ml_engineer", "pct": 50}, {"id": "tailored", "pct": 60}]}


MIRROR = {"jd_skills_allowed": True}


class MirrorJdTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_the_setting_comes_from_rules_toml(self):
        self.assertFalse(rules.for_profile("alex-rivera")["jd_skills_allowed"])
        path = config.PROFILES / "alex-rivera" / "rules.toml"
        path.write_text(path.read_text().replace("jd_skills_allowed = false", "jd_skills_allowed = true"))
        self.assertTrue(rules.for_profile("alex-rivera")["jd_skills_allowed"])

    def test_every_required_jd_skill_must_be_on_the_resume(self):
        inp = types.SimpleNamespace(pid="alex-rivera", profile_rules=MIRROR, base_id="ml_engineer")
        fails, info = checks.check_ats(inp, SCORES)
        self.assertEqual(info["uncovered_known"], ["Rust"])          # required + missing; dbt is only preferred
        self.assertTrue(any(f.name == "ats_coverage" and "Required JD skills" in f.message for f in fails))

    def test_a_jd_skill_is_never_dropped_by_the_free_fix(self):
        content = {"summary": "", "jobs": [], "skills": [{"category": "Data", "items": ["Snowflake", "Teradata"]}]}
        report = checks.Report([checks.Failure(5, "unknown_skills", "x")],
                               {"honesty": {"unknown_terms": ["Snowflake", "Teradata"]}})
        out = repair.mechanical(content, report, {}, "We use Snowflake and Airflow.")
        self.assertEqual(out.content["skills"][0]["items"], ["Snowflake"])
        self.assertTrue(repair.named_in("IaC", "Infrastructure as Code (IaC) with Terraform"))
        self.assertFalse(repair.named_in("Rust", "We trust our team"))

    def test_repairs_may_add_jd_skills_only_when_mirroring(self):
        self.assertIn("Rust", repair.allowed_keywords(SCORES, [], [], jd_skills=True))
        self.assertNotIn("Rust", repair.allowed_keywords(SCORES, [], [], jd_skills=False))

    def test_the_prompt_says_mirror_the_jds_hard_skills(self):
        prof, base = example_profile()
        measure = types.SimpleNamespace(chars_per_line=110, bullets_end_with_period=False, skill_lines={})
        text = prompts.build(prof, base, measure, "jd " * 40, "Acme", "ML Engineer", SCORES)
        self.assertNotIn("MIRROR THE JD'S HARD SKILLS", text)
        self.assertIn("only those in the base resumes or the KNOWN SKILLS", text)
        prof.rules = {**prof.rules, **MIRROR}
        text = prompts.build(prof, base, measure, "jd " * 40, "Acme", "ML Engineer", SCORES)
        self.assertIn("MIRROR THE JD'S HARD SKILLS", text)
        self.assertIn("add EVERY required and preferred keyword", text)


if __name__ == "__main__":
    unittest.main()
