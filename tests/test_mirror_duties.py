"""rules.toml `mirror_duties`: in the same tailoring call the model lists the JD's 3-5 core responsibilities and domain
nouns, maps each to the ONE bullet whose real work matches it, rewords that bullet with the JD's phrasing and records
mirrored / unmatched. checks.check_mirror fails a mirrored phrase on a bullet whose core fact is not in the fact bank
or the confirmed facts. Without the rule the prompt and schema are unchanged."""
import types
import unittest

from resume_tailor import checks, docxio, pipeline, profiles, prompts, rules
from tests.helpers import PID, TempEnv

MIRROR = {**rules.for_profile(PID), **rules.parse('mirror_duties = true\nverbs_unique = true\nmax_from_to = 4\n'
                                                   '[employer_domains]\n"Lumen Freight" = ["logistics", "freight"]\n')}
SHIPPED = ("Shipped a retrieval tool with LangChain and pgvector that answers dispatcher questions from 40,000 shipping "
           "documents, with a 600 question evaluation set run on every release.")


class MirrorCheck(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.env.__enter__()
        self.prof = profiles.load(PID)
        self.base = self.prof.bases[0].structure

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _inp(self, mirrored, domain=()):
        return types.SimpleNamespace(pid=PID, profile_rules=MIRROR, role="Machine Learning Engineer", location="",
                                     notes=(), base_structure=self.base, known_skills=list(self.prof.known_skills),
                                     fact_bank=self.prof.fact_bank_resume, fact_bank_all=self.prof.fact_bank,
                                     mirror={"mirrored": mirrored, "jd_domain": list(domain)})

    def _tailored(self, job: int, index: int, text: str) -> docxio.Structure:
        t = docxio.Structure(self.base.name, self.base.headline, self.base.contact, self.base.summary, [], [], [], [],
                             list(self.base.sections), 1)
        t.skills = self.base.skills
        t.jobs = [{**j, "bullets": list(j["bullets"])} for j in self.base.jobs]
        t.jobs[job]["bullets"][index] = text
        return t

    def _fails(self, text, entry, job=0, index=1, domain=()):
        t = self._tailored(job, index, text)
        fails, info = checks.check_mirror(self._inp([entry], domain), t)
        return [f.message for f in fails], info

    def test_a_true_reword_with_the_jds_phrase_passes(self):
        text = ("Shipped a retrieval tool with LangChain and pgvector that answers dispatcher questions from 40,000 "
                "logistics documents, building evaluation pipelines with a 600 question set run on every release.")
        entry = {"responsibility": "build evaluation pipelines", "bullet_id": "1.2",
                 "phrase_used": "building evaluation pipelines"}
        fails, info = self._fails(text, entry, domain=("logistics",))
        self.assertEqual(fails, [])
        self.assertTrue(info[0]["ok"])
        self.assertEqual(info[0]["at"], [0, 1])

    def test_a_number_from_another_fact_fails(self):
        text = SHIPPED.replace("dispatcher questions", "questions for 410 dispatchers") + " Owning models end to end."
        entry = {"responsibility": "own models end to end", "bullet_id": "1.2", "phrase_used": "Owning models end to end"}
        fails, _ = self._fails(text, entry)
        self.assertTrue(fails and "410" in fails[0], fails)
        self.assertIn("mirrored", fails[0])
        self.assertIn("count this responsibility as not covered", fails[0])

    def test_a_tool_from_another_job_fails(self):
        text = SHIPPED.replace("pgvector", "pgvector on Snowflake").replace("with a 600", "building evaluation "
                                                                           "pipelines with a 600")
        entry = {"responsibility": "build evaluation pipelines", "bullet_id": "1.2",
                 "phrase_used": "building evaluation pipelines"}
        fails, _ = self._fails(text, entry)
        self.assertTrue(fails and "Snowflake" in fails[0], fails)

    def test_a_new_claim_fails(self):
        text = ("Led a fraud detection team of eight data scientists building credit risk scorecards for consumer "
                "lending portfolios, owning models end to end.")
        entry = {"responsibility": "own models end to end", "bullet_id": "1.2", "phrase_used": "owning models end to end"}
        fails, _ = self._fails(text, entry)
        self.assertTrue(fails and "own words" in fails[0], fails)

    def test_a_domain_the_work_never_touched_fails(self):
        text = SHIPPED.replace("shipping documents", "healthcare documents")
        entry = {"responsibility": "answer questions", "bullet_id": "1.2", "phrase_used": "answers dispatcher questions"}
        fails, _ = self._fails(text, entry, domain=("healthcare",))
        self.assertTrue(fails and "healthcare" in fails[0], fails)
        ok_text = SHIPPED.replace("shipping documents", "logistics documents")
        self.assertEqual(self._fails(ok_text, entry, domain=("logistics",))[0], [])

    def test_a_phrase_no_longer_on_any_bullet_is_not_covered_and_not_a_failure(self):
        entry = {"responsibility": "own models end to end", "bullet_id": "1.2", "phrase_used": "owning models end to end"}
        fails, info = self._fails(SHIPPED, entry)
        self.assertEqual(fails, [])
        self.assertFalse(info[0]["ok"])
        self.assertIsNone(info[0]["at"])

    def test_a_reordered_bullet_is_found_by_its_phrase(self):
        t = self._tailored(0, 0, self.base.jobs[0]["bullets"][0])
        t.jobs[0]["bullets"] = list(reversed(t.jobs[0]["bullets"]))
        entry = {"responsibility": "x", "bullet_id": "1.1", "phrase_used": "predicts load volume for 1,200 routes"}
        self.assertEqual(checks.find_mirrored(t, self.base, entry), (0, 3))

    def test_a_skill_said_another_way_is_held_a_named_tool_is_not(self):
        facts = "Fine-tuned an 8B LLM with LoRA for tool calls. Built the Bedrock SQL agent and its evaluation set."
        for term, want in (("LLM Fine-tuning", True), ("LLM Evaluation", True), ("Generative AI", True),
                           ("Kubernetes", False), ("Model Deployment", False), ("observability", False)):
            with self.subTest(term):
                self.assertEqual(checks._held(term, facts), want)
        short = "Built the eval set for the LLM assistant and deployed the reranker."         # abbreviations hold
        for term, want in (("LLM Evaluation", True), ("Model Deployment", False), ("Reranker deployment", True)):
            with self.subTest(term, facts="short"):
                self.assertEqual(checks._held(term, short), want)

    def test_employer_facts_reads_every_section_of_one_employer(self):
        bank = self.prof.fact_bank + "\n## Part 2\n### Lumen Freight\n- The dispatch team had 45 people.\n"
        facts = checks.employer_facts(bank, "Lumen Freight, USA")
        self.assertIn("1,200 routes", facts)                     # the main section
        self.assertIn("45 people", facts)                        # Part 2
        self.assertNotIn("23 clinics", facts)                    # another employer


class Summary(unittest.TestCase):
    def test_covered_and_gaps(self):
        content = {"jd_responsibilities": ["partner with product", "own models end to end", "build evals",
                                           "work in a regulated environment", "mentor juniors"],
                   "jd_domain": ["logistics"], "unmatched": ["mentor juniors"]}
        report = types.SimpleNamespace(info={"mirror": [
            {"responsibility": "partner with product", "bullet_id": "1.2", "phrase_used": "p", "at": [0, 1], "ok": True},
            {"responsibility": "own models end to end", "bullet_id": "2.1", "phrase_used": "o", "at": [1, 0], "ok": True},
            {"responsibility": "build evals", "bullet_id": "1.3", "phrase_used": "b", "at": [0, 2], "ok": True},
            {"responsibility": "work in a regulated environment", "bullet_id": "2.2", "phrase_used": "w", "at": None,
             "ok": False}]})
        got = pipeline.mirror_summary(content, report)
        self.assertEqual((got["covered"], got["total"]), (3, 5))
        self.assertEqual(got["unmatched"], ["work in a regulated environment", "mentor juniors"])
        self.assertEqual(len(got["mirrored"]), 3)
        self.assertIsNone(pipeline.mirror_summary({}, report))


class PromptAndSchema(unittest.TestCase):
    def test_the_rule_adds_the_mirror_section_and_schema(self):
        self.assertIs(prompts.schema_for(MIRROR), prompts.MIRROR_SCHEMA)
        for key in ("jd_responsibilities", "jd_domain", "mirrored", "unmatched"):
            self.assertIn(key, prompts.MIRROR_SCHEMA["required"])
        base = types.SimpleNamespace(jobs=[{"company": "Lumen Freight, USA", "bullets": ["Built a thing."]},
                                           {"company": "Brightline Health, USA", "bullets": ["Shipped it."]}])
        text = "\n".join(prompts._mirror_lines(base, MIRROR))
        for want in ("3 to 5 core responsibilities", "ONE base bullet", "phrase_used", "unmatched",
                     "- 1.1 (Lumen Freight): Built a thing.", "- 2.1 (Brightline Health): Shipped it.",
                     "no new claims, projects, tools or numbers", "team player", "Lumen Freight: logistics, freight",
                     "opening verbs unique", "at most 4"):
            self.assertIn(want, text)

    def test_without_the_rule_the_schema_and_prompt_stay_as_they_were(self):
        plain = rules.for_profile(PID)
        self.assertFalse(plain["mirror_duties"])
        self.assertIs(prompts.schema_for(plain), prompts.SCHEMA)
        self.assertNotIn("jd_responsibilities", prompts.SCHEMA["properties"])


if __name__ == "__main__":
    unittest.main()
