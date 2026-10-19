"""The built-in keyword model: whole-word matching, required vs nice-to-have, scores and the best base."""
import json
import unittest

from resume_tailor import ats, config
from tests.helpers import JD_TEXT, TempEnv, example_profile, real_scores


class FindTests(unittest.TestCase):
    def test_whole_words_and_aliases(self):
        found = ats.find("Built pipelines in Python and PySpark on AWS; Postgres and K8s; sklearn models")
        for name in ("Python", "PySpark", "AWS", "PostgreSQL", "Kubernetes", "scikit-learn"):
            self.assertIn(name, found)
        self.assertNotIn("Apache Spark", ats.find("PySpark only"))         # not a substring hit

    def test_short_names_need_their_own_case(self):
        self.assertIn("R", ats.find("Statistics in R and Python"))
        self.assertNotIn("R", ats.find("R&D budget, r squared"))
        self.assertIn("Go", ats.find("Services written in Go."))
        self.assertNotIn("Go", ats.find("go to market"))

    def test_symbols_in_names(self):
        found = ats.find("C++ and C# services, CI/CD with GitHub Actions, A/B tests, Node.js")
        for name in ("C++", "C#", "CI/CD", "GitHub Actions", "A/B Testing", "Node.js"):
            self.assertIn(name, found)


class ExtractTests(unittest.TestCase):
    def test_required_and_preferred_sections(self):
        jd = ("Requirements\n- Python and SQL\n- Kubernetes\n\nNice to have\n- Databricks\n- Kafka\n")
        terms = {t["name"]: t for t in ats.extract(jd)}
        self.assertTrue(terms["Python"]["need"])
        self.assertTrue(terms["Kubernetes"]["need"])
        self.assertFalse(terms["Databricks"]["need"])
        self.assertFalse(terms["Apache Kafka"]["need"])
        self.assertGreater(terms["Python"]["weight"], terms["Databricks"]["weight"])

    def test_a_plus_line_is_preferred(self):
        terms = {t["name"]: t for t in ats.extract("Skills\n- Python\n- Experience with Airflow is a plus\n")}
        self.assertTrue(terms["Python"]["need"])
        self.assertFalse(terms["Apache Airflow"]["need"])


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.prof, self.base = example_profile()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_shape_the_checks_read(self):
        scores = real_scores(self.prof, self.base, self.base.text)
        for key in ("terms", "scores", "best_id", "found", "categories", "jd_found", "label", "vendor_label"):
            self.assertIn(key, scores)
        by_id = {s["id"]: s for s in scores["scores"]}
        self.assertEqual(by_id["tailored"]["pct"], by_id[self.base.id]["pct"])
        self.assertEqual(scores["best_id"], self.base.id)
        self.assertIn("Snowflake", by_id[self.base.id]["missing"])
        self.assertIn("Snowflake", scores["found"]["known"])               # known_skills.txt lists it

    def test_adding_a_missing_skill_raises_the_score(self):
        plain = real_scores(self.prof, self.base, self.base.text)
        more = real_scores(self.prof, self.base, self.base.text + "\nSnowflake, Redshift, Datadog")
        pct = lambda s: next(x["pct"] for x in s["scores"] if x["id"] == "tailored")
        self.assertGreater(pct(more), pct(plain))

    def test_a_jd_with_no_known_skill_scores_everyone_100(self):
        scores = ats.score("We want kindness and punctuality.", [{"id": "a", "label": "A", "text": "Python"}])
        self.assertEqual(scores["scores"][0]["pct"], 100)

    def test_your_own_lexicon_terms(self):
        (config.DATA / "lexicon.json").write_text(json.dumps({"Mine": {"Quarkus": ["quarkus framework"]}}))
        self.assertIn("Quarkus", ats.find("Services on the Quarkus framework"))
        self.assertEqual(ats.category_of("Quarkus"), "Mine")

    def test_title_breaks_a_tie_between_bases(self):
        resumes = [{"id": "ds", "label": "Data Scientist", "text": "Python"},
                   {"id": "ml", "label": "ML Engineer", "text": "Python", "families": ["Machine Learning Engineer"]}]
        self.assertEqual(ats.score("Python", resumes, title="Senior ML Engineer")["best_id"], "ml")
        self.assertEqual(ats.score("Python", resumes, title="Data Scientist II")["best_id"], "ds")

    def test_jd_text_helper(self):
        self.assertIn("Python", ats.find(JD_TEXT))


if __name__ == "__main__":
    unittest.main()
