"""With headline_from_job, the title under the name is always one of five plain titles, picked by the posting
title's family; a title naming both AI and ML is "AI/ML Engineer"; unclear -> the base's own title. The titles
below are made-up examples of the shapes postings come in."""
import unittest

from resume_tailor import headline

EXAMPLES = [
    ("Senior Data Scientist, Health Plans", "Data Scientist"),
    ("Embedded AI Tools Engineer", "AI Engineer"),
    ("AI Product Engineer (Copilot)", "AI Engineer"),
    ("Senior Machine Learning Engineer II", "Machine Learning Engineer"),
    ("Applied Scientist, Robotics", "Machine Learning Engineer"),
    ("Senior Analytics Engineer", "Data Engineer"),
    ("Pharmacometrics Data Scientist", "Data Scientist"),
]
BOTH_AI_AND_ML = ["AI/ML Engineer", "AI ML Engineer", "AI & ML Engineer", "Applied AI ML, Senior Associate",
                  "Machine Learning & AI Engineer", "Next-Gen AI/ML Systems Engineer, Data Fusion"]
MORE_TITLES = [
    ("Staff Data Scientist, Ads", "Data Scientist"),
    ("Data Science Manager", "Data Scientist"),
    ("Decision Scientist II", "Data Scientist"),
    ("Data Scientist, GenAI", "Data Scientist"),                    # the data role is checked first
    ("Data Engineer III", "Data Engineer"),
    ("Senior Data Engineer, Platform", "Data Engineer"),
    ("ETL Developer", "Data Engineer"),
    ("Data Platform Engineer", "Data Engineer"),
    ("MLOps Engineer (Hybrid) #1234", "Machine Learning Engineer"),
    ("Computer Vision Engineer (C++)", "Machine Learning Engineer"),
    ("Deep Learning Engineer", "Machine Learning Engineer"),
    ("NLP Engineer", "Machine Learning Engineer"),
    ("Software Engineer, Applied AI", "AI Engineer"),
    ("Agentic AI Engineer, Remote", "AI Engineer"),
    ("Generative AI Engineer", "AI Engineer"),
    ("LLM Engineer", "AI Engineer"),
    ("GenAI Developer", "AI Engineer"),
    ("Robotic AI Application Engineer (Automation)", "AI Engineer"),
]


class HeadlineRule(unittest.TestCase):
    def test_the_examples(self):
        for title, want in EXAMPLES:
            with self.subTest(title=title):
                self.assertEqual(headline.for_job(title, "X"), want)

    def test_a_title_naming_both_ai_and_ml_is_ai_ml_engineer(self):
        for title in BOTH_AI_AND_ML:
            with self.subTest(title=title):
                self.assertEqual(headline.for_job(title, "X"), "AI/ML Engineer")

    def test_more_titles_land_in_their_family(self):
        for title, want in MORE_TITLES:
            with self.subTest(title=title):
                self.assertEqual(headline.for_job(title, "X"), want)

    def test_always_one_of_the_five_titles_or_the_base_title(self):
        for title, _ in EXAMPLES + MORE_TITLES:
            self.assertIn(headline.for_job(title, "X"), headline.TITLES)

    def test_unclear_falls_back_to_the_base_title(self):
        for title in ("Senior Software Engineer", "Product Manager", "", "Senior Data Analyst", "HTML Developer",
                      "Data Entry Clerk", "Email Marketing Specialist"):
            with self.subTest(title=title):
                self.assertEqual(headline.for_job(title, "Data Engineer"), "Data Engineer")


if __name__ == "__main__":
    unittest.main()
