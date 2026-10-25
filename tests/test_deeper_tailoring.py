"""rules.toml summary_structure, skills_add_first and spare_swaps (tailorchecks.py): the summary written fresh per job,
JD skills added to their row first and a row trimmed only by its [drop_order], approved spare bullets (facts.md
Part 3) swapped in without asking, the number gate on Part 1 or Part 3, JD skills first in each row, and the answer's
swaps / skills_added / skills_removed. Made-up data only."""
import types
import unittest

from resume_tailor import checks, docxio, pipeline, profiles, prompts, rules, skillrules, tailorchecks
from tests.helpers import PID

RULES = rules.parse('''
headline_from_job = true
summary_years = "6 years"
summary_structure = { verb = "shipping", max_numbers = 2, min_employers = 2 }
summary_industries = ["logistics", "healthcare", "retail"]
skill_rows_fixed = true
skills_add_first = true
locked_skills = ["PyTorch", "MLflow", "SQL"]
spare_swaps = true
anchor_bullets = ["Lumen Freight forecasting", "Brightline follow-up models"]
max_swaps = 2

[drop_order]
"ML & MLOps" = ["scikit-learn", "Airflow", "pandas"]
"Data" = ["Snowflake", "dbt"]
''')
B_LF = ["Built a demand forecasting service in PyTorch and Airflow that predicts load volume for 1,200 routes, cutting "
        "empty truck miles 14%.",
        "Moved training to SageMaker with MLflow and Docker, taking retraining from two days to five hours for weekly "
        "releases.",
        "Set up drift monitoring and alerts for 9 production models on Kubernetes with Prometheus and Grafana "
        "dashboards."]
B_BH = ["Trained XGBoost models that flag patients likely to miss follow-up visits, lifting scheduled follow-ups 11% "
        "across 23 clinics.",
        "Rebuilt the feature pipeline in Python and dbt on Snowflake, cutting the nightly run from 3 hours to 35 "
        "minutes.",
        "Ran A/B tests on appointment reminder timing and wrote the analysis templates the analysts still use."]
B_NW = ["Built churn models in scikit-learn for retail clients, with plain-language driver summaries for 150 store "
        "managers."]
SPARE_LINES = {
    "Lumen Freight": [
        "- L1 (evaluation, quality): Ran a 600 question evaluation set on every release of the dispatcher retrieval "
        "tool, lifting answer accuracy from 71% to 89%.",
        "- L2 (variant of the drift bullet; monitoring): Paged the on-call engineer within 10 minutes when input data "
        "shifted, catching two broken pipelines before they reached pricing."],
    "Brightline Health": [
        "- H1 (privacy; replaces reminder or pipeline bullet): De-identified training data under HIPAA rules for 2 "
        "research partners, with every export reviewed before it left the clinic network."],
}
FACT_BANK = ("# Fact bank\n\n## Lumen Freight, USA (Mar 2023 to present)\n\n- " + B_LF[0]
             + "\n\n## Part 2: interview-only facts\n\n- 4,000 loads a week.\n\n"
             "## Part 3: spare bullets\n\n"
             + "\n\n".join(f"### {job} (spare bullets)\n\n" + "\n".join(lines) for job, lines in SPARE_LINES.items())
             + "\n")
SR = skillrules.for_rules(RULES, FACT_BANK)
SPARES = {s.id: s.text for s in SR.spares}
ROWS = [("ML & MLOps", "PyTorch, MLflow, scikit-learn, Airflow, Docker, pandas"), ("Data", "SQL, Snowflake, dbt")]
JD = "We want MLflow, Airflow and Kubeflow for pipelines, and SQL."
SUMMARY = ("Machine Learning Engineer with 6 years shipping forecasting and retrieval tools across logistics and "
           "healthcare. At Lumen Freight, built a demand forecasting service for 1,200 routes. Brightline Health work "
           "covered patient follow-up models with clinic teams.")
TITLE = "Machine Learning Engineer"


def _struct(summary: str, jobs: list, skills=None) -> docxio.Structure:
    s = docxio.Structure("Alex Rivera", TITLE, "contact", summary, [3], [], [], [], ["Summary", "Skills"], 1)
    s.jobs = [{"company": c, "dates": "Jan 2021 – Jul 2023", "title": TITLE, "bullets": list(b),
               "bullet_paras": [], "header_para": 0, "title_para": None} for c, b in jobs]
    s.skills = [docxio.SkillLine(cat, items, 0, n) for n, (cat, items) in enumerate(skills or [])]
    return s


def _jobs(lf=B_LF, bh=B_BH, nw=B_NW):
    return [("Lumen Freight, USA", lf), ("Brightline Health, USA", bh), ("Northwind Analytics, USA", nw)]


def _scores(page_terms=("PyTorch", "MLflow", "Airflow", "SQL"), terms=("Kubeflow",)):
    mlops = "MLOps"
    return {"found": {"ml_engineer": {"PyTorch": 1, "MLflow": 1, "Airflow": 1}, "known": {"Kubeflow": 1},
                      "tailored": {t: 1 for t in page_terms}, "skills_row_0": {"PyTorch": 1, "MLflow": 1},
                      "skills_row_1": {"SQL": 1}},
            "categories": {"PyTorch": mlops, "MLflow": mlops, "Kubeflow": mlops, "SQL": "Data"},
            "jd_found": {"MLflow": 1, "Airflow": 1, "Kubeflow": 1, "SQL": 1},
            "terms": [{"name": t, "need": True} for t in terms]}


def _inp(scores=None, answer=None, profile_rules=RULES):
    base = _struct(SUMMARY, _jobs(), ROWS)
    return types.SimpleNamespace(pid=PID, profile_rules=profile_rules, role=TITLE, location="", base_structure=base,
                                 base_id="ml_engineer", skill_text=FACT_BANK, fact_bank_all=FACT_BANK,
                                 answer=answer or {}, jd_text=JD, scores=scores or _scores(), known_skills=["Kubeflow"],
                                 notes=())


def _job_rules() -> dict:
    return rules.for_job(rules.for_profile(PID, RULES), TITLE, TITLE)


def _names(fails) -> list:
    return [f.name for f in fails]


class RulesFile(unittest.TestCase):
    def test_locked_drop_order_anchors_swaps_and_industries_come_from_rules_toml(self):
        self.assertEqual(SR.locked, ("PyTorch", "MLflow", "SQL"))
        self.assertEqual(SR.drop_order["ML & MLOps"], ("scikit-learn", "Airflow", "pandas"))
        self.assertEqual(SR.anchors, ("Lumen Freight forecasting", "Brightline follow-up models"))
        self.assertEqual(SR.max_swaps, 2)
        self.assertEqual(SR.industries, ("logistics", "healthcare", "retail"))
        self.assertEqual(RULES["summary_structure"], {"verb": "shipping", "max_numbers": 2, "min_employers": 2})

    def test_spares_and_their_targets_come_from_part_3(self):
        self.assertEqual([(s.id, s.job, s.targets) for s in SR.spares],
                         [("L1", "Lumen Freight", ()), ("L2", "Lumen Freight", ("drift",)),
                          ("H1", "Brightline Health", ("reminder", "pipeline"))])
        self.assertEqual([s.id for s in SR.spares_of("Lumen Freight, USA")], ["L1", "L2"])
        self.assertEqual(skillrules.spares("# no Part 3 here\n- L1 (x): y"), ())

    def test_bad_keys_are_refused_with_what_is_wrong(self):
        cases = {"max_swaps must be a whole number": "max_swaps = -1",
                 "summary_structure takes verb": 'summary_structure = { verb = 3 }',
                 "[drop_order] maps a skills row": '[drop_order]\n"ML" = "pandas"',
                 "can't be used with summary_keep": 'summary_structure = { verb = "shipping" }\nsummary_keep = [2, 3]',
                 "add skill_rows_fixed = true": "skills_add_first = true"}
        for want, text in cases.items():
            with self.subTest(want=want):
                with self.assertRaises(rules.RulesError) as err:
                    rules.parse(text)
                self.assertIn(want, str(err.exception))

    def test_every_rule_is_off_by_default(self):
        r = rules.for_profile(PID, {})
        self.assertFalse(r["spare_swaps"] or r["skills_add_first"] or r["summary_structure"])
        self.assertEqual(tailorchecks.run(_inp(profile_rules={}), _struct(SUMMARY, _jobs(), ROWS)), ([], {}))


class NumberGate(unittest.TestCase):
    def test_part_1_and_part_3_numbers_are_allowed_part_2_is_not(self):
        prof = types.SimpleNamespace(fact_bank=FACT_BANK)
        text = profiles.Profile.fact_bank_resume.fget(prof)
        self.assertIn("1,200 routes", text)
        self.assertIn("71% to 89%", text)
        self.assertNotIn("4,000 loads", text)

    def test_the_prompt_names_part_3_only_with_spares(self):
        on = {**_job_rules(), "fact_bank_numbers": True}
        self.assertIn("or in a spare bullet (its Part 3)", prompts._numbers_line(on))
        self.assertNotIn("Part 3", prompts._numbers_line({**on, "spare_swaps": False}))


class SummaryStructure(unittest.TestCase):
    def _fails(self, summary: str) -> list:
        page = _struct(summary, _jobs(), ROWS)
        return tailorchecks.summary_problems(_inp(), page, _job_rules(), SR)

    def test_the_example_shape_passes(self):
        self.assertEqual(self._fails(SUMMARY), [])

    def test_each_broken_rule_is_named(self):
        cases = {
            "the summary's sentence 1 must open": SUMMARY.replace("6 years shipping", "6 years building"),
            "industries only": SUMMARY.replace("logistics and", "finance and"),
            "states 3 numbers": SUMMARY.replace("for 1,200 routes", "for 1,200 routes and 14% fewer miles, 9 models"),
            "names 1 employer": "Machine Learning Engineer with 6 years shipping forecasting across logistics. At Lumen "
                                "Freight, built a demand forecasting service. It predicts load volume for every route "
                                "each day.",
            "open the same way": SUMMARY.replace("Brightline Health work covered", "At Brightline Health, built"),
            "repeats a bullet's sentence": SUMMARY.replace(
                "At Lumen Freight, built a demand forecasting service for 1,200 routes.", B_LF[0]),
        }
        for want, summary in cases.items():
            with self.subTest(want=want):
                fails = self._fails(summary)
                self.assertTrue(fails and set(_names(fails)) == {"summary_structure"}, [f.message for f in fails])
                self.assertTrue(any(want in f.message for f in fails), [f.message for f in fails])
                self.assertTrue(all("summary" in f.message for f in fails))    # the repair sends the summary back

    def test_the_prompt_asks_for_the_shape(self):
        lines = "\n".join(prompts._shape_lines(_job_rules()))
        self.assertIn('sentence 1 opens "Machine Learning Engineer with 6 years shipping"', lines)
        self.assertIn('"across" + industries from: logistics, healthcare, retail', lines)
        self.assertIn("at most 2 numbers besides the years; at least 2 employer(s) named", lines)


class SkillsAddFirst(unittest.TestCase):
    def _fails(self, ml_row: str, page_terms=None) -> tuple:
        page = _struct(SUMMARY, _jobs(), [(ROWS[0][0], ml_row), ROWS[1]])
        return tailorchecks.skill_problems(_inp(_scores(page_terms) if page_terms else _scores()), page, SR)

    def test_a_jd_skill_added_and_the_first_drop_order_skill_out_passes(self):
        fails, info = self._fails("MLflow, Airflow, Kubeflow, PyTorch, Docker, pandas", ("MLflow", "Airflow", "Kubeflow"))
        self.assertEqual(fails, [])
        self.assertEqual(info["added"], {"ML & MLOps": ["Kubeflow"]})
        self.assertEqual(info["removed"], {"ML & MLOps": ["scikit-learn"]})

    def test_each_wrong_removal_fails(self):
        add = "Kubeflow, "
        cases = {"the LOCKED skill 'PyTorch'": add + "MLflow, scikit-learn, Airflow, Docker, pandas",
                 "'Airflow', which the JD names": add + "PyTorch, MLflow, scikit-learn, Docker, pandas",
                 "'Docker', which is not in this row's DROP ORDER": add + "PyTorch, MLflow, scikit-learn, Airflow, "
                                                                          "pandas",
                 "the DROP ORDER takes 'scikit-learn' out first": add + "PyTorch, MLflow, scikit-learn, Airflow, Docker",
                 "but added no JD skill": "PyTorch, MLflow, Airflow, Docker, pandas"}
        for want, row in cases.items():
            with self.subTest(want=want):
                fails, _ = self._fails(row, ("MLflow", "Airflow", "Kubeflow"))
                self.assertTrue(any(want in f.message for f in fails), [f.message for f in fails])
                self.assertTrue(all(f.name == "skills_add_first" and "skills '" in f.message for f in fails))

    def test_a_jd_skill_left_out_while_a_removable_skill_stays_fails(self):
        fails, info = self._fails(ROWS[0][1])
        self.assertEqual(info["missing_jd"], ["Kubeflow"])
        self.assertEqual(len(fails), 1, [f.message for f in fails])
        self.assertIn("skills 'ML & MLOps' is missing 'Kubeflow'", fails[0].message)
        self.assertIn("'scikit-learn' (DROP ORDER, not in the JD)", fails[0].message)

    def test_python_puts_jd_skills_first_then_the_base_order(self):
        base = {"skills": [{"category": "ML & MLOps", "items": [x.strip() for x in ROWS[0][1].split(",")]}]}
        content = {"skills": [{"category": "ML & MLOps",
                               "items": ["pandas", "PyTorch", "Kubeflow", "Docker", "MLflow", "Airflow"]}]}
        out = tailorchecks.jd_first(content, base, JD, {"Kubeflow": 1})
        self.assertEqual(out["skills"][0]["items"], ["Kubeflow", "MLflow", "Airflow", "PyTorch", "Docker", "pandas"])
        self.assertEqual(content["skills"][0]["items"][0], "pandas", "the input is never changed")

    def test_python_trims_a_row_only_by_its_drop_order(self):
        base = {"skills": [{"category": "ML & MLOps", "items": [x.strip() for x in ROWS[0][1].split(",")]}]}
        kf, pt, mf, sk, af, dk, pd = ("Kubeflow", "PyTorch", "MLflow", "scikit-learn", "Airflow", "Docker", "pandas")
        cases = {   # the answer's row -> the row Python keeps (Airflow is named by the JD, so never dropped)
            "one out of DROP ORDER": ([kf, pt, mf, sk, af, pd], [kf, pt, mf, af, pd, dk]),
            "two out, the JD's one among them": ([kf, pt, mf, dk, pd], [kf, pt, mf, dk, af]),
            "nothing added": ([pt, mf, af, dk, pd], [pt, mf, af, dk, pd, sk]),
        }
        for name, (row, want) in cases.items():
            with self.subTest(name):
                content = {"skills": [{"category": "ML & MLOps", "items": row}]}
                out = tailorchecks.drop_by_order(content, base, SR, JD, {kf: 1})["skills"][0]["items"]
                self.assertEqual(out, want)
                page = _struct(SUMMARY, _jobs(), [(ROWS[0][0], ", ".join(out)), ROWS[1]])
                fails, _ = tailorchecks.skill_problems(_inp(_scores((mf, af, kf))), page, SR)
                self.assertFalse([f for f in fails if "removed" in f.message], [f.message for f in fails])
                self.assertEqual(content["skills"][0]["items"], row, "the input is never changed")
        right = {"skills": [{"category": "ML & MLOps", "items": [kf, pt, mf, af, dk, pd]}]}
        self.assertIs(tailorchecks.drop_by_order(right, base, SR, JD, {})["skills"][0], right["skills"][0])

    def test_an_added_skill_is_never_its_base_twin_listed_again(self):
        base = {"skills": [{"category": "Languages & Data", "items": ["Python", "SQL", "SQL Server", "Kafka", "Snowflake"]},
                           {"category": "LLMs", "items": ["Claude API", "LangChain"]}]}
        jd = "Stream with Apache Kafka into PostgreSQL. Claude experience a plus."
        answer = {"skills": [{"category": "Languages & Data", "items": ["Apache Kafka", "PostgreSQL", "Python", "SQL",
                                                                        "SQL Server", "Kafka"]},
                             {"category": "LLMs", "items": ["Claude", "Claude API", "LangChain"]}]}
        out = tailorchecks.merge_same(answer, base, jd, {"Apache Kafka": 1, "PostgreSQL": 1})
        self.assertEqual(out["skills"][0]["items"], ["PostgreSQL", "Python", "SQL", "SQL Server", "Apache Kafka"])
        self.assertEqual(len([x for x in out["skills"][1]["items"] if "Claude" in x]), 1)
        self.assertTrue(tailorchecks.same_skill("Kafka", "Apache Kafka"))
        self.assertFalse(tailorchecks.same_skill("PySpark", "Spark SQL"))
        page = _struct(SUMMARY, _jobs(), [("Languages & Data", ", ".join(out["skills"][0]["items"])), ROWS[1]])
        inp = types.SimpleNamespace(**{**vars(_inp()), "base_structure": _struct(SUMMARY, _jobs(), [
            ("Languages & Data", "Python, SQL, SQL Server, Kafka, Snowflake"), ROWS[1]]), "jd_text": jd})
        _, info = tailorchecks.skill_problems(inp, page, SR)
        self.assertEqual(info["removed"], {"Languages & Data": ["Snowflake"]})       # Kafka is kept, as Apache Kafka

    def test_a_skill_the_fact_bank_names_is_owned_and_every_other_jd_skill_is_a_gap(self):
        scores = {**_scores(), "found": {**_scores()["found"], "facts": {"ETL": 1}},
                  "terms": [{"name": n, "need": True} for n in ("ETL", "Kubeflow", "REST APIs")]}
        block = prompts._ats_block(scores, {"Kubeflow"}, add_first=True)
        self.assertIn("or the fact bank; SKILLS: ADD FIRST", block)
        self.assertRegex(block, r"owns .*: ETL, Kubeflow\n")
        self.assertIn("never add it to Skills or a bullet, not even as a near synonym: REST APIs", block)
        page = _struct(SUMMARY, _jobs(), [(ROWS[0][0], ROWS[0][1] + ", ETL"), ROWS[1]])
        tailored = {**scores, "found": {**scores["found"], "tailored": {**scores["found"]["tailored"], "ETL": 1}}}
        inp = types.SimpleNamespace(**vars(_inp(tailored)), all_base_texts=[], fact_bank="")
        _, info = checks.check_honesty(inp, page, tailored)
        self.assertNotIn("ETL", info["unknown_terms"])
        without = {**tailored, "found": {k: v for k, v in tailored["found"].items() if k != "facts"}}
        self.assertIn("ETL", checks.check_honesty(inp, page, without)[1]["unknown_terms"])
        prof = types.SimpleNamespace(id=PID, rules=RULES, fact_bank=FACT_BANK, fact_bank_resume="Part 1 and 3")
        self.assertEqual(pipeline._facts_text(prof), {"facts": "Part 1 and 3"})
        self.assertEqual(pipeline._facts_text(types.SimpleNamespace(id=PID, rules={}, fact_bank=FACT_BANK,
                                                                    fact_bank_resume="x")), {})

    def test_the_prompt_lists_the_drop_order_and_the_locked_skills(self):
        lines = "\n".join(prompts._shape_lines(_job_rules()))
        self.assertIn("SKILLS: ADD FIRST", lines)
        self.assertIn("DROP ORDER (ML & MLOps: scikit-learn, Airflow, pandas; Data: Snowflake, dbt)", lines)
        self.assertIn("never a LOCKED skill (PyTorch, MLflow, SQL)", lines)

    def test_the_skill_items_check_stays_for_a_person_without_add_first(self):
        base = _struct(SUMMARY, _jobs(), ROWS)
        page = _struct(SUMMARY, _jobs(), [(ROWS[0][0], "PyTorch, MLflow"), ROWS[1]])
        self.assertTrue(checks.skill_item_problems(base, page))

    def test_the_rows_are_scored_for_the_row_a_missing_skill_belongs_in(self):
        struct = _struct(SUMMARY, _jobs(), ROWS)
        self.assertEqual({k: v for k, v in checks.era_texts(struct, PID, RULES).items() if k.startswith("skills_row_")},
                         {"skills_row_0": ROWS[0][1], "skills_row_1": ROWS[1][1]})
        self.assertFalse([k for k in checks.era_texts(struct, PID, {}) if k.startswith("skills_row_")])


class SpareSwaps(unittest.TestCase):
    def _run(self, lf=B_LF, bh=B_BH, answer=None, sr=SR):
        page = _struct(SUMMARY, _jobs(lf, bh), ROWS)
        return tailorchecks.swap_problems(_inp(answer=answer), page, sr, answer or {})

    def test_a_spare_in_place_of_a_plain_bullet_passes_and_is_tagged_with_its_reason(self):
        answer = {"swaps": [{"job": "Lumen Freight", "out_bullet": "1.2", "spare_id": "L1",
                             "reason": "JD stresses evaluation"}]}
        fails, info = self._run(lf=[B_LF[0], SPARES["L1"], B_LF[2]], answer=answer)
        self.assertEqual(fails, [])
        self.assertEqual([(s["spare_id"], s["in_at"], s["out_bullet"], s["reason"]) for s in info["swaps"]],
                         [("L1", "1.2", "1.2", "JD stresses evaluation")])
        self.assertEqual(info["declared_not_found"], [])

    def test_the_base_has_no_swaps(self):
        self.assertEqual(self._run(), ([], {"swaps": [], "declared_not_found": []}))

    def test_a_reworded_spare_is_still_found(self):
        reworded = SPARES["L1"].replace("Ran", "Used").replace("lifting", "raising")
        _, info = self._run(lf=[B_LF[0], reworded, B_LF[2]])
        self.assertEqual([s["spare_id"] for s in info["swaps"]], ["L1"])

    def test_an_anchor_out_fails(self):
        fails, _ = self._run(lf=[SPARES["L1"], B_LF[1], B_LF[2]])
        self.assertEqual(_names(fails), ["spare_swaps"])
        self.assertIn("the anchor bullet 'Lumen Freight forecasting' is off the page", fails[0].message)
        self.assertIn("bullet 1 of role 1 (spare L1)", fails[0].message)

    def test_a_variant_only_replaces_its_own_bullet(self):
        fails, _ = self._run(lf=[B_LF[0], SPARES["L2"], B_LF[2]])
        self.assertEqual(_names(fails), ["spare_swaps"])
        self.assertIn("spare L2, which may replace only the drift bullet", fails[0].message)
        ok, info = self._run(lf=[B_LF[0], B_LF[1], SPARES["L2"]])
        self.assertEqual(ok, [])
        self.assertEqual(info["swaps"][0]["out_bullet"], "1.3")

    def test_replaces_one_of_two_named_bullets(self):
        ok, info = self._run(bh=[B_BH[0], B_BH[1], SPARES["H1"]])            # "replaces reminder or pipeline bullet"
        self.assertEqual(ok, [])
        self.assertEqual(info["swaps"][0]["out_bullet"], "2.3")

    def test_more_swaps_than_the_rules_allow_fail(self):
        one = skillrules.for_rules({**RULES, "max_swaps": 1}, FACT_BANK)
        fails, _ = self._run(lf=[B_LF[0], SPARES["L1"], SPARES["L2"]], sr=one)
        self.assertTrue(any("takes 2 spare bullets; at most 1 per job" in f.message for f in fails),
                        [f.message for f in fails])

    def test_a_spare_in_another_job_fails(self):
        fails, _ = self._run(bh=[B_BH[0], B_BH[1], SPARES["L1"]])
        self.assertTrue(any("bullet 3 of role 2 is spare L1 of Lumen Freight" in f.message for f in fails),
                        [f.message for f in fails])

    def test_a_number_two_bullets_state_fails(self):
        fails, _ = self._run(lf=[B_LF[0], SPARES["L1"], B_LF[2]],
                             bh=[B_BH[0], B_BH[1], B_BH[2].replace("the analysis templates", "600 question templates")])
        self.assertTrue(any("(spare L1) states '600 " in f.message and "no two bullets state the same number"
                            in f.message for f in fails), [f.message for f in fails])

    def test_number_keys(self):
        self.assertEqual(tailorchecks.number_keys("lifted completed runs from 82% to 96%."), {"82%", "96%"})
        self.assertEqual(tailorchecks.number_keys("clear 7-10 invoices daily, $5-7K"), set())
        self.assertEqual(tailorchecks.number_keys("the 12 GPU nodes"), {"12 gpu"})


class Contract(unittest.TestCase):
    def test_the_prompt_lists_the_spares_and_the_schema_carries_the_swaps(self):
        lines = "\n".join(prompts._tailor_lines(_struct(SUMMARY, _jobs(), ROWS), _job_rules(), SR))
        self.assertIn("Spares for Lumen Freight (job 1):", lines)
        self.assertIn("- L2 (variant of the drift bullet; monitoring): Paged the on-call engineer", lines)
        self.assertIn("never an anchor: Lumen Freight forecasting, Brightline follow-up models", lines)
        self.assertIn("swaps: [{job (company), out_bullet", lines)
        schema = prompts.schema_for(_job_rules())
        self.assertEqual(schema["properties"]["swaps"]["items"]["required"], ["job", "out_bullet", "spare_id", "reason"])
        self.assertIn("skills_removed", schema["required"])
        self.assertNotIn("swaps", prompts.schema_for(rules.for_profile(PID, {}))["properties"])

    def test_answer_keys_carry_the_swaps_to_the_checks(self):
        content = {"mirrored": [], "swaps": [{"spare_id": "L1"}], "skills_added": [], "summary": "s"}
        self.assertEqual(pipeline.answer_keys(content)["swaps"], [{"spare_id": "L1"}])
        self.assertNotIn("summary", pipeline.answer_keys(content))

    def test_the_review_panel_gets_the_swaps_and_skill_changes(self):
        report = types.SimpleNamespace(info={"tailor": {"swaps": {"swaps": [{"spare_id": "L1", "in_at": "1.2"}]},
                                                        "skills": {"added": {"ML & MLOps": ["Kubeflow"]},
                                                                   "removed": {"ML & MLOps": ["scikit-learn"]},
                                                                   "missing_jd": []}}})
        out = pipeline.tailor_summary({"swaps": [{"spare_id": "L1"}]}, report)
        self.assertEqual(out["swaps"], [{"spare_id": "L1", "in_at": "1.2"}])
        self.assertEqual(out["skills_removed"], {"ML & MLOps": ["scikit-learn"]})
        self.assertEqual(out["declared"]["swaps"], [{"spare_id": "L1"}])
        self.assertIsNone(pipeline.tailor_summary({}, types.SimpleNamespace(info={})))

    def test_all_three_run_together_when_on(self):
        page = _struct(SUMMARY.replace("6 years shipping", "6 years building"), _jobs(lf=[SPARES["L1"], B_LF[1],
                                                                                        B_LF[2]]), ROWS)
        fails, info = tailorchecks.run(_inp(), page)
        self.assertEqual({"summary_structure", "spare_swaps", "skills_add_first"}, set(_names(fails)))
        self.assertEqual(set(info), {"skills", "swaps"})


if __name__ == "__main__":
    unittest.main()
