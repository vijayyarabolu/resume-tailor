"""The title under the name. Alex's rules.toml says headline_from_job: it is one of five plain titles by the
posting's family (headline.py), the base's own title when the family is unclear, and the summary opens
"<headline> with 6 years". A profile with a fixed `headline` keeps that title whatever the job, and nobody's
summary opens with two titles."""
import copy
import types
import unittest

from resume_tailor import checks, config, docxio, pipeline, profiles, prompts, rules
from tests.helpers import TempEnv, add_second_base

FIXED = 'headline = "AI Engineer"\nsummary_years = "6 years"\nsummary_must_contain = ["6 years"]\n'


def _writing_failures(pid: str, base, summary: str, role: str = "") -> list:
    tstruct = copy.deepcopy(base.structure)
    tstruct.summary = summary
    prof = profiles.load(pid)
    inp = types.SimpleNamespace(pid=pid, profile_rules=prof.rules, base_id=base.id, base_measure=types.SimpleNamespace(
        bullets_end_with_period=True), role=role, base_structure=base.structure, location="",
        all_base_texts=[b.text for b in prof.bases])
    return [f for f in checks.check_writing(inp, tstruct) if f.name in ("summary_start", "summary_contains")]


class TitleRule(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv(people=("alex-rivera", "sam-lee")).__enter__()
        add_second_base()                                    # a Data Scientist base next to the ML Engineer one
        (config.PROFILES / "sam-lee" / "rules.toml").write_text(FIXED)
        profiles.reload()
        self.alex = profiles.load("alex-rivera")
        self.sam = profiles.load("sam-lee")

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _rules(self, prof, base, role: str) -> dict:
        return rules.for_job(rules.for_profile(prof.id, prof.rules), role, base.structure.headline)

    def test_headline_follows_the_job_family(self):
        prules = rules.for_profile("alex-rivera", self.alex.rules)
        self.assertFalse(prules["headline_may_change"])
        self.assertTrue(prules["headline_from_job"])
        ml = self.alex.base("ml_engineer")
        jr = self._rules(self.alex, ml, ml.structure.headline)          # a posting titled like the base: it passes
        self.assertEqual((jr["headline"], jr["summary_start"]),
                         ("Machine Learning Engineer", "Machine Learning Engineer with 6 years"))
        self.assertTrue(ml.structure.summary.startswith(jr["summary_start"]))
        jr = self._rules(self.alex, ml, "Embedded AI Tools Engineer")
        self.assertEqual((jr["headline"], jr["summary_start"]), ("AI Engineer", "AI Engineer with 6 years"))
        jr = self._rules(self.alex, self.alex.base("data_scientist"), "Applied Scientist, Robotics")
        self.assertEqual(jr["headline"], "Machine Learning Engineer", "the job's family, not the base's")
        jr = self._rules(self.alex, ml, "Machine Learning & AI Engineer")
        self.assertEqual(jr["summary_start"], "AI/ML Engineer with 6 years")
        jr = self._rules(self.alex, self.alex.base("data_scientist"), "Senior Software Engineer")
        self.assertEqual(jr["headline"], "Data Scientist", "family unclear: that base's title")

    def test_a_fixed_headline_holds_whatever_the_job_title(self):
        base = self.sam.bases[0]
        prules = rules.for_profile("sam-lee", self.sam.rules)
        self.assertFalse(prules["headline_may_change"])
        self.assertFalse(prules["headline_from_job"])
        for role in ("Data Scientist", "Applied AI Engineer", "Staff Data Scientist, Ads", "Product Manager"):
            with self.subTest(role=role):
                jr = self._rules(self.sam, base, role)
                self.assertEqual(rules.headline_of(jr, base.structure.headline), "AI Engineer")
                self.assertEqual(jr["summary_start"], "AI Engineer with 6 years")
        content = {"headline": "Data Scientist", "summary": "Data Scientist with 6 years of ML. More."}
        fixed = pipeline.locked_headline(content, base, self._rules(self.sam, base, "Data Scientist"))
        self.assertEqual((fixed["headline"], fixed["summary"]), ("AI Engineer", "AI Engineer with 6 years of ML. More."))
        inp = types.SimpleNamespace(pid="sam-lee", profile_rules=self.sam.rules, role="Data Scientist",
                                    base_structure=base.structure, location="",
                                    all_base_texts=[b.text for b in self.sam.bases])
        other = copy.deepcopy(base.structure)
        other.headline = "Data Scientist"
        self.assertTrue(checks.check_layout(inp, other), "a check fails any other title")
        right = copy.deepcopy(base.structure)
        right.headline = "AI Engineer"
        self.assertEqual(checks.check_layout(inp, right), [])

    def test_the_summary_opens_with_the_headline_once_never_two_titles(self):
        self.assertIn("once", checks.summary_title_problem("Data Scientist",
                                                           "AI Engineer Data Scientist with 3+ years of ML."))
        self.assertIsNotNone(checks.summary_title_problem("AI Tools Engineer", "AI Engineer with 4 years of x."))
        self.assertIsNone(checks.summary_title_problem("AI Tools Engineer", "AI Tools Engineer with 4 years of x."))
        self.assertIsNone(checks.summary_title_problem("AI Engineer", "Builds LLM systems; 3+ years."))
        base = self.alex.base("ml_engineer")
        tstruct = copy.deepcopy(base.structure)
        tstruct.summary = ("AI Engineer Machine Learning Engineer with 6 years"
                           + base.structure.summary.split("with 6 years", 1)[1])
        inp = types.SimpleNamespace(pid="alex-rivera", profile_rules=self.alex.rules, base_id=base.id,
                                    role="Machine Learning Engineer", location="",
                                    base_measure=types.SimpleNamespace(bullets_end_with_period=True),
                                    base_structure=base.structure, all_base_texts=[base.text])
        names = {f.name for f in checks.check_writing(inp, tstruct)}
        self.assertTrue({"summary_title", "summary_start"} <= names, names)
        tstruct.summary = base.structure.summary
        self.assertNotIn("summary_title", {f.name for f in checks.check_writing(inp, tstruct)})

    def test_locked_headline_sets_the_jobs_headline_and_fixes_the_summary_opening(self):
        base = self.alex.base("ml_engineer")
        jr = self._rules(self.alex, base, "Embedded AI Tools Engineer")
        content = {"headline": "Machine Learning Engineer",
                   "summary": "Machine Learning Engineer with 6 years of building LLM apps. More."}
        out = pipeline.locked_headline(content, base, jr)
        self.assertEqual(out["headline"], "AI Engineer")
        self.assertEqual(out["summary"], "AI Engineer with 6 years of building LLM apps. More.")
        self.assertEqual(content["headline"], "Machine Learning Engineer", "input is not mutated")
        free = pipeline.locked_headline(content, base, {"headline_may_change": True})
        self.assertIs(free, content)
        odd = {"headline": "x", "summary": "Builds things. With 6 years."}
        self.assertEqual(pipeline.locked_headline(odd, base, jr)["summary"], odd["summary"], "left for the checks")

    def test_layout_check_requires_the_jobs_headline(self):
        base = self.alex.base("ml_engineer")
        other = copy.deepcopy(base.structure)
        inp = types.SimpleNamespace(pid="alex-rivera", profile_rules=self.alex.rules, location="",
                                    role="Senior Machine Learning Engineer II", base_structure=base.structure,
                                    all_base_texts=[b.text for b in self.alex.bases])
        self.assertEqual(checks.check_layout(inp, other), [])
        inp.role = "Embedded AI Tools Engineer"
        problems = [f.message for f in checks.check_layout(inp, other)]
        self.assertTrue(any("'AI Engineer'" in p for p in problems), problems)
        self.assertEqual(docxio.layout_diffs(base.structure, base.structure, headline_may_change=False), [])

    def test_summary_opening_must_match_the_headline_check_7(self):
        base = self.alex.base("ml_engineer")
        rest = base.structure.summary.split(" with 6 years", 1)[1]
        role = "Applied AI Engineer"
        self.assertEqual(_writing_failures("alex-rivera", base, "AI Engineer with 6 years" + rest, role), [])
        names = [f.name for f in _writing_failures("alex-rivera", base, base.structure.summary, role)]
        self.assertIn("summary_start", names)

    def test_prompts_state_the_title_rule(self):
        base = self.alex.base("ml_engineer")
        measure = types.SimpleNamespace(chars_per_line=110, bullets_end_with_period=False, skill_lines={})
        text = prompts.build(self.alex, base, measure, "jd " * 40, "Acme", "Staff Analytics Engineer", None)
        self.assertIn('the title under the name is exactly "Data Engineer"', text)
        self.assertIn('The summary must open with "Data Engineer with 6 years"', text)
        jr = prompts.job_rules(self.alex, base, "Staff Analytics Engineer")
        repair_text = prompts._rule_lines(jr, measure, base.structure, base.id)
        self.assertTrue(any('"Data Engineer with 6 years"' in line for line in repair_text))


if __name__ == "__main__":
    unittest.main()
