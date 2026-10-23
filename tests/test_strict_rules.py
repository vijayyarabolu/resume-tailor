"""The stricter rules.toml keys for one master resume: a short title list, summary sentences kept, bullet shapes,
numbers only from this base and facts.md above Part 2, JD skills the person lacks listed as Gaps, fixed skill rows
and a report-only hiring-manager pass. All of them are off by default, and a profile without them is judged as
before."""
import types
import unittest

from resume_tailor import checks, docxio, headline, pipeline, profiles, prompts, repair, rules
from tests.helpers import PID, TempEnv


def _struct(summary: str, jobs: list, skills=None) -> docxio.Structure:
    s = docxio.Structure("NAME", "AI Engineer", "contact", summary, [3], [], [], [], ["Summary", "Skills"], 1)
    s.jobs = [{"company": c, "dates": "Jan 2021 – Jul 2023", "title": "AI Engineer", "bullets": list(b),
               "bullet_paras": [], "header_para": 0, "title_para": None} for c, b in jobs]
    s.skills = [docxio.SkillLine(cat, items, 0, n) for n, (cat, items) in enumerate(skills or [])]
    return s


def _words(n: int, verb: str = "Built", extra: str = "") -> str:
    body = ["thing"] * (n - 1 - len(extra.split()))
    return " ".join([verb] + body + extra.split()).strip() + "."


STRICT = {**rules.for_profile(PID), **rules.parse("""
bullet_words = [20, 32]
headline_from_job = true
summary_years = "6 years"
headline_titles = ["AI Engineer", "AI/ML Engineer", "Machine Learning Engineer"]
summary_keep = [2, 3]
verbs_unique = true
short_bullets = [27, 4]
max_at_top = 5
bullet_plain = true
max_from_to = 4
jd_gaps = true
fact_bank_numbers = true
skill_rows_fixed = true
hm_review = true
""")}
PLAIN = rules.for_profile(PID)
KEYS = ("summary_keep", "verbs_unique", "short_bullets", "bullet_plain", "max_from_to", "jd_gaps", "fact_bank_numbers",
        "skill_rows_fixed", "hm_review", "headline_titles", "max_at_top", "mirror_duties", "employer_domains")


class Titles(unittest.TestCase):
    def test_a_title_list_maps_every_posting_onto_it(self):
        cases = {
            "AI Agent Engineer": "AI Engineer", "LLM Engineer": "AI Engineer", "Generative AI Engineer": "AI Engineer",
            "Applied AI Engineer": "AI Engineer", "AI/ML Engineer": "AI/ML Engineer",
            "Machine Learning Engineer, LLM": "AI/ML Engineer", "Machine Learning Engineer": "Machine Learning Engineer",
            "Senior MLOps Engineer": "Machine Learning Engineer", "Applied Scientist": "Machine Learning Engineer",
            "Senior Data Scientist, GenAI": "AI Engineer", "Data Engineer": "AI Engineer",
            "Software Engineer": "AI Engineer", "Member of Technical Staff": "AI Engineer",
        }
        for role, want in cases.items():
            with self.subTest(role=role):
                jr = rules.for_job(STRICT, role, "AI Engineer")
                self.assertEqual(jr["headline"], want)
                self.assertEqual(jr["summary_start"], f"{want} with 6 years")
                self.assertIn(jr["headline"], STRICT["headline_titles"])

    def test_without_a_title_list_the_five_titles_are_unchanged(self):
        self.assertEqual(headline.for_job("Data Scientist, Marketing", "AI Engineer"), "Data Scientist")
        self.assertEqual(headline.for_job("Data Engineer", "AI Engineer"), "Data Engineer")
        for key in KEYS:
            self.assertFalse(PLAIN[key], key)


class Parse(unittest.TestCase):
    def test_the_keys_parse_and_bad_shapes_are_refused(self):
        self.assertEqual(STRICT["summary_keep"], (2, 3))
        self.assertEqual(STRICT["short_bullets"], (27, 4))
        self.assertEqual(STRICT["headline_titles"], ("AI Engineer", "AI/ML Engineer", "Machine Learning Engineer"))
        got = rules.parse('[employer_domains]\n"Lumen Freight" = ["logistics"]\n')
        self.assertEqual(got["employer_domains"], {"Lumen Freight": ("logistics",)})
        for bad in ("short_bullets = 4", 'headline_titles = "AI Engineer"', "summary_keep = [3, 2]",
                    'employer_domains = "x"'):
            with self.subTest(bad):
                with self.assertRaises(rules.RulesError):
                    rules.parse(bad)


class BulletShape(unittest.TestCase):
    def _fails(self, bullets, prules=STRICT):
        tstruct = _struct("s", [("Lumen Freight, USA", bullets[:3]), ("Northwind Analytics, USA", bullets[3:])])
        return {f.name: f.message for f in checks._bullet_shape_problems(tstruct, prules)}

    def test_good_bullets_pass(self):
        bullets = [_words(24, "Architected"), _words(26, "Built"), _words(30, "Designed"), _words(22, "Trained"),
                   _words(27, "Wired")]
        self.assertEqual(self._fails(bullets), {})

    def test_an_opening_verb_used_twice_names_both_bullets(self):
        bullets = [_words(24, "Built"), _words(25, "Designed"), _words(26, "built"), _words(22, "Trained"),
                   _words(23, "Wired")]
        msg = self._fails(bullets)["verbs_unique"]
        self.assertIn("bullet 1 of role 1", msg)
        self.assertIn("bullet 3 of role 1", msg)

    def test_lengths_must_vary(self):
        bullets = [_words(30, v) for v in ("Built", "Designed", "Shipped", "Trained", "Wired")]
        self.assertIn("short_bullets", self._fails(bullets))
        at_top = [_words(32, v) for v in ("Built", "Designed", "Shipped", "Trained", "Wired", "Added")]
        tstruct = _struct("s", [("Lumen", at_top)])
        names = [f.name for f in checks._bullet_shape_problems(tstruct, STRICT)]
        self.assertIn("at_top", names, "max_at_top: no more than 5 bullets at exactly 32 words")

    def test_semicolons_dashes_and_parentheses_fail_hyphen_ranges_pass(self):
        ok = _words(22, "Built", "clear 7-10 invoices daily, up from 3-5 and $5-7K")
        for bad in (";", "—", "–", "(SQL)"):
            with self.subTest(bad=bad):
                bullets = [_words(22, "Built", f"one {bad} two"), _words(23, "Designed"), _words(24, "Shipped"),
                           _words(22, "Trained"), ok.replace("Built", "Wired", 1)]
                self.assertIn("bullet 1 of role 1", self._fails(bullets).get("bullet_punct", ""))
        bullets = [ok, _words(23, "Designed"), _words(24, "Shipped"), _words(22, "Trained"), _words(21, "Wired")]
        self.assertNotIn("bullet_punct", self._fails(bullets))

    def test_at_most_four_from_x_to_y(self):
        ft = "cut p95 from 8 seconds to 260 ms"
        bullets = [_words(24, v, ft) for v in ("Built", "Designed", "Shipped", "Trained", "Wired")]
        self.assertIn("from_to", self._fails(bullets))
        self.assertEqual(checks.from_to_count("Cut spend to $3K from $5-7K by caching repeated prompts and routing "
                                              "routine lookups to the 8B model"), 0, "a long gap is not the phrasing")
        self.assertNotIn("from_to", self._fails([_words(24, v, ft) for v in ("Built", "Designed", "Shipped",
                                                                              "Trained")] + [_words(22, "Wired")]))

    def test_without_the_rules_bullets_are_not_judged_by_them(self):
        bullets = [_words(24, "Built", "one; two (x)")] * 5
        self.assertEqual(self._fails(bullets, PLAIN), {})


class FactBankPart2(unittest.TestCase):
    def test_numbers_come_from_above_part_two_only(self):
        with TempEnv():
            folder = profiles.folder(PID)
            facts = folder / "facts.md"
            facts.write_text(facts.read_text() + "\n## Part 2\n- The dispatch team had 330 people.\n")
            profiles.reload()
            prof = profiles.load(PID)
            self.assertNotIn("## Part 2", prof.fact_bank_resume)
            self.assertIn("1,200 routes", prof.fact_bank_resume)
            self.assertNotIn("330", checks._numbers(prof.fact_bank_resume), "an interview-only number")
            self.assertIn("330", checks._numbers(prof.fact_bank))


class FactsAndGaps(unittest.TestCase):
    """check_honesty with a synthetic score: numbers from the fact bank, JD skills the person lacks listed as gaps."""

    def _inp(self, bank: str, known=("Python",), terms=("Python", "Rust", "Go")):
        scores = {"found": {"ai_engineer": {"Python": 1}, "known": {"Python": 1}, "tailored": {"Python": 1}},
                  "categories": {}, "jd_found": {t: 1 for t in terms},
                  "terms": [{"name": t, "need": True} for t in terms]}
        base = _struct("AI Engineer with 4 years.", [("Lumen", ["Built x."])])
        return types.SimpleNamespace(pid=PID, profile_rules=STRICT, role="AI Engineer", location="",
                                     base_structure=base, known_skills=list(known), notes=(), all_base_texts=[bank, "330 communities"],
                                     skill_text="330 communities", fact_bank=bank), scores

    def test_a_number_outside_part_one_fails_even_if_another_base_has_it(self):
        with TempEnv():
            inp, scores = self._inp("AI Engineer with 4 years. Across 30+ communities, 98% kept.")
            t = _struct("AI Engineer with 4 years.", [("Lumen", ["Kept 98% across 330 communities."])])
            fails, info = checks.check_honesty(inp, t, scores)
            self.assertIn("numbers", [f.name for f in fails])
            self.assertTrue(any("330" in b for b in info["bad_numbers"]))
            t2 = _struct("AI Engineer with 4 years.", [("Lumen", ["Kept 98% across 30+ communities."])])
            self.assertNotIn("numbers", [f.name for f in checks.check_honesty(inp, t2, scores)[0]])

    def test_jd_skills_the_person_lacks_are_gaps_and_never_added(self):
        with TempEnv():
            inp, scores = self._inp("Built x.", known=("Python", "Go"))
            fails, info = checks.check_honesty(inp, _struct("AI Engineer with 4 years.", [("Lumen", ["Built x."])]),
                                               scores)
            self.assertEqual(info["gaps"], ["Rust"])
            self.assertEqual(info["added_from_jd"], [])
            scores["found"]["tailored"]["Rust"] = 1
            fails, info = checks.check_honesty(inp, _struct("x", [("Lumen", ["Built Rust."])]), scores)
            self.assertIn("unknown_skills", [f.name for f in fails], "a gap written in fails the tools gate")

    def test_a_cloud_vendor_prefix_does_not_make_a_known_skill_a_gap(self):
        with TempEnv():
            inp, scores = self._inp("Built x.", known=("Python", "Glue"), terms=("Python", "AWS Glue", "Azure Synapse"))
            info = checks.check_honesty(inp, _struct("AI Engineer with 4 years.", [("Lumen", ["Built x."])]), scores)[1]
            self.assertEqual(info["gaps"], ["Azure Synapse"])

    def test_a_note_confirming_a_gap_takes_it_off_the_list(self):
        with TempEnv():
            inp, scores = self._inp("Built x.")
            inp.notes = ("I've used Rust: add it to the right Skills row.",)
            scores["found"]["tailored"]["Rust"] = 1
            fails, info = checks.check_honesty(inp, _struct("x", [("Lumen", ["Built x."])]), scores)
            self.assertEqual(info["confirmed_by_note"], ["Rust"])
            self.assertNotIn("Rust", info["gaps"])
            self.assertNotIn("unknown_skills", [f.name for f in fails])


class SkillRowsAndSummary(unittest.TestCase):
    def _line(self, text: str, y: float):
        return types.SimpleNamespace(text=text, x0=0.0, x1=500.0, y0=y)

    def test_row_line_counts_are_exact_and_no_dangling_word(self):
        t = _struct("s", [("Lumen", [])], [("LLMs & Agents", "Claude API, LangGraph"), ("Cloud & DevOps", "AWS")])
        t.sections = ["Summary", "Skills", "Work Experience"]
        lines = [self._line("Skills", 1), self._line("LLMs & Agents: Claude API,", 2), self._line("LangGraph", 3),
                 self._line("Cloud & DevOps: AWS", 4), self._line("Work Experience", 5)]
        base_lines = {"LLMs & Agents": 2, "Cloud & DevOps": 1}
        out = {f.name: f.message for f in checks._skill_row_problems(t, lines, base_lines, base_lines, 2)}
        self.assertIn("dangling", out)
        self.assertIn("'LLMs & Agents'", out["dangling"])
        out = {f.name: f.message for f in checks._skill_row_problems(t, lines, base_lines,
                                                                       {"LLMs & Agents": 1, "Cloud & DevOps": 2}, 2)}
        self.assertIn("skill_row_lines", out)

    def test_mechanical_restores_summary_sentences_and_row_names(self):
        base = {"summary": "AI Engineer with 4 years doing X. First ML hire at Lumen, where A. Built B at Brightline, "
                           "and C at Northwind.",
                "skills": [{"category": "LLMs & Agents", "items": ["a"]}, {"category": "Cloud & DevOps", "items": ["b"]}]}
        content = {"summary": "AI/ML Engineer with 4 years doing Y. First ML hire at Lumen, changed. Other.",
                   "skills": [{"category": "GenAI", "items": ["a"]}, {"category": "Cloud", "items": ["b"]}],
                   "jobs": [{"company": "Lumen", "bullets": ["Cut 7–10 invoices."]}]}
        report = checks.Report([checks.Failure(7, "summary_keep", "x"), checks.Failure(2, "skill_rows", "x"),
                                checks.Failure(7, "bullet_punct", "bullet 1 of role 1 contains '–'")])
        out = repair.restore_master(content, report, STRICT, base)
        self.assertEqual(out.content["summary"], "AI/ML Engineer with 4 years doing Y. First ML hire at Lumen, where A. "
                                                 "Built B at Brightline, and C at Northwind.")
        self.assertEqual([s["category"] for s in out.content["skills"]], ["LLMs & Agents", "Cloud & DevOps"])
        self.assertEqual(out.content["jobs"][0]["bullets"][0], "Cut 7-10 invoices.")
        self.assertEqual(content["skills"][0]["category"], "GenAI", "the input is never changed")

    def test_a_dropped_or_renamed_master_skill_fails_reorder_and_additions_pass(self):
        base = _struct("s", [("Lumen", [])], [("LLMs & Agents", "Claude API, LangGraph, Multi-Agent Workflows"),
                                            ("ML", "PyTorch, Ollama, vLLM")])
        ok = _struct("s", [("Lumen", [])], [("LLMs & Agents", "Multi-Agent Workflows, Claude API, LangGraph"),
                                          ("ML", "vLLM, PyTorch, Ollama, Reinforcement Learning")])
        self.assertEqual(checks.skill_item_problems(base, ok), [])
        bad = _struct("s", [("Lumen", [])], [("LLMs & Agents", "Multi-Agent Orchestration, Claude API, LangGraph"),
                                           ("ML", "PyTorch, Reinforcement Learning")])
        out = checks.skill_item_problems(base, bad)
        self.assertEqual([f.name for f in out], ["skill_items", "skill_items"])
        self.assertIn("skills 'LLMs & Agents'", out[0].message)
        self.assertIn("Multi-Agent Workflows", out[0].message)
        self.assertIn("Ollama, vLLM", out[1].message)

    def test_mechanical_puts_master_skills_back_in_claudes_order(self):
        base = {"summary": "s", "skills": [{"category": "ML", "items": ["PyTorch", "Ollama", "vLLM", "NLP"]}]}
        content = {"summary": "s", "jobs": [],
                   "skills": [{"category": "ML", "items": ["vLLM", "Anomaly Detection", "PyTorch"]}]}
        report = checks.Report([checks.Failure(2, "skill_items", "skills 'ML' dropped master items: Ollama, NLP")])
        out = repair.restore_master(content, report, STRICT, base)
        self.assertEqual(out.content["skills"][0]["items"], ["vLLM", "PyTorch", "Ollama", "NLP"])
        self.assertTrue(any("Anomaly Detection" in n for n in out.notes))
        self.assertEqual(content["skills"][0]["items"], ["vLLM", "Anomaly Detection", "PyTorch"], "input unchanged")

    def test_the_plan_sends_every_named_bullet_but_the_first_verb_back(self):
        content = {"summary": "s", "skills": [], "jobs": [{"company": "Lumen", "bullets": ["Built a.", "Built b.",
                                                                                         "Made c."]}]}
        report = checks.Report([checks.Failure(7, "verbs_unique", "the opening verb 'built' is used 2 times (bullet 1 "
                                                                  "of role 1, bullet 2 of role 1); ...")])
        plan = repair.plan(report, content, types.SimpleNamespace(skill_lines={}, summary_lines=0), STRICT, set())
        self.assertEqual([(i.kind, i.job, i.index) for i in plan.items], [("bullet", 1, 2)])


class Prompts(unittest.TestCase):
    def test_the_rules_prompt_lines(self):
        lines = "\n".join(prompts._shape_lines(STRICT)) + prompts._numbers_line(STRICT)
        for want in ("sentences 2-3 stay the base's", "reorder bullets WITHIN a job", "opening verb",
                     "never drop or rename a base skill",
                     "at least 4 bullets have 27 words or fewer", "at most 5 have exactly 32", "At most 4 \"from X",
                     "FACT BANK above its Part 2"):
            self.assertIn(want, lines)

    def test_without_the_rules_the_prompt_has_none_of_them(self):
        self.assertEqual(prompts._shape_lines(PLAIN), [])
        self.assertIn("the base resumes or the fact bank", prompts._numbers_line(PLAIN))

    def test_hm_prompt_is_report_only(self):
        prof = types.SimpleNamespace(name="Alex Rivera")
        text = prompts.build_hm(prof, {"headline": "AI Engineer", "summary": "s", "skills": [], "jobs": []}, "JD",
                                "Acme", "AI Engineer")
        self.assertIn("Report only", text)
        self.assertIn("Never suggest a fact, tool, number", text)


class HiringManagerPass(unittest.TestCase):
    def _ctx(self, pid: str):
        prof = types.SimpleNamespace(id=pid, name="Alex Rivera", config_dir=None)
        job = {"id": 7, "jd_text": "JD", "company": "Acme", "role": "AI Engineer"}
        return types.SimpleNamespace(prules={"hm_review": pid == PID}, prof=prof, job=job, conn=None,
                                     settings={"model": "m", "effort": "medium"})

    @staticmethod
    def _res(ok: bool, output=None):
        return types.SimpleNamespace(ok=ok, output=output, error="" if ok else "usage limit", model="m",
                                     as_dict=lambda: {}, raw="", stderr="", kind="ok" if ok else "limit")

    def test_report_saved_status_untouched_and_skipped_without_the_rule(self):
        from resume_tailor import db
        calls, writes = [], []

        def runner(prompt, schema, **kw):
            calls.append(schema)
            return self._res(True, {"reads_human": True, "clear_fit": False, "score": 7,
                                    "notes": ["move the retrieval bullet up"]})
        saved = db.update, db.event, db.set_status
        db.update = lambda *a, **k: writes.append(k)
        db.event = lambda *a, **k: None
        db.set_status = lambda *a, **k: self.fail("a report never changes the status")
        try:
            with TempEnv():
                st = pipeline.State({"attempts": []}, content={"headline": "AI Engineer", "summary": "s"})
                pipeline.hm_review(self._ctx(PID), st, runner, "ready")
                self.assertEqual(st.result["hm_review"]["score"], 7)
                self.assertFalse(st.result["hm_review"]["clear_fit"])
                self.assertEqual(calls, [prompts.HM_SCHEMA])
                self.assertTrue(all("status" not in w for w in writes))
                st2 = pipeline.State({"attempts": []}, content={"summary": "s"})
                pipeline.hm_review(self._ctx("sam-lee"), st2, runner, "ready")
                self.assertNotIn("hm_review", st2.result)
                st3 = pipeline.State({"attempts": []}, content={"summary": "s"})
                pipeline.hm_review(self._ctx(PID), st3, lambda *a, **k: self._res(False), "needs_you")
                self.assertNotIn("hm_review", st3.result)
                self.assertEqual(len(calls), 1, "without hm_review a resume costs no hiring-manager call")
        finally:
            db.update, db.event, db.set_status = saved


if __name__ == "__main__":
    unittest.main()
