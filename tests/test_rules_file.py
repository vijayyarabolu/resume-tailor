"""rules.toml: parsing, defaults, the errors a person sees, and that an edit is used at once."""
import unittest

from resume_tailor import profiles, rules
from tests.helpers import PID, TempEnv


class ParseTests(unittest.TestCase):
    def test_ranges_lists_and_verbs(self):
        got = rules.parse('bullet_words = [15, 25]\nsummary_sentences = 3\nera_jobs = ["Acme"]\n'
                          '[verbs]\n"Acme" = ["Built", "Ran"]\n')
        self.assertEqual(got["bullet_words"], (15, 25))
        self.assertEqual(got["summary_sentences"], (3, 3))
        self.assertEqual(got["era_jobs"], ("Acme",))
        self.assertEqual(got["verbs"], {"Acme": ("Built", "Ran")})

    def test_headline_from_job_locks_the_title(self):
        self.assertFalse(rules.parse("headline_from_job = true")["headline_may_change"])
        self.assertFalse(rules.parse('headline = "Data Engineer"')["headline_may_change"])

    def test_errors_say_what_is_wrong(self):
        for text, words in (("bullet_words = [25, 15]", "[low, high]"), ("foo = 1", "unknown key"),
                            ("era_jobs = 3", "list of strings"), ("bullet_words = [", "not valid TOML")):
            with self.subTest(text=text), self.assertRaisesRegex(rules.RulesError, words.replace("[", r"\[")):
                rules.parse(text)

    def test_defaults_fill_the_rest(self):
        merged = rules.for_profile("nobody", {"bullet_words": (10, 20)})
        self.assertEqual(merged["bullet_words"], (10, 20))
        self.assertEqual(merged["min_bullet_lines"], 2)
        self.assertFalse(merged["jd_skills_allowed"])

    def test_banned_words_add_to_the_built_in_list(self):
        rx = rules.banned_re({"banned_words": ("rockstar",)})
        self.assertTrue(rx.search("a rockstar engineer"))
        self.assertTrue(rx.search("leveraged synergy"))

    def test_fixed_headline_with_years_sets_the_summary_opening(self):
        prules = rules.for_profile("x", rules.parse('headline = "Data Engineer"\nsummary_years = "5 years"'))
        self.assertEqual(rules.for_job(prules, "Anything", "Base")["summary_start"], "Data Engineer with 5 years")


class ProfileFileTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_the_example_rules_load(self):
        prules = rules.for_profile(PID, profiles.load(PID).rules)
        self.assertEqual(prules["era_jobs"], ("Northwind",))
        self.assertEqual(prules["location"], "Portland, OR")
        self.assertTrue(prules["headline_from_job"])

    def test_an_edit_is_seen_by_the_next_load(self):
        profiles.write_file(PID, "rules.toml", "bullet_words = [10, 40]\n")
        self.assertEqual(profiles.load(PID).rules["bullet_words"], (10, 40))

    def test_a_bad_file_is_refused_and_the_old_one_kept(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.write_file(PID, "rules.toml", "bullet_words = 'many'\n")
        self.assertEqual(profiles.load(PID).rules["bullet_words"], (20, 32))

    def test_profile_toml_needs_a_name(self):
        with self.assertRaisesRegex(profiles.ProfileError, "needs a name"):
            profiles.write_file(PID, "profile.toml", "years = 3\n")


if __name__ == "__main__":
    unittest.main()
