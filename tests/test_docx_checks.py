"""docxio round trip on the example base and the 8 checks: base-vs-base passes, injected faults fail with the
right check number."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from resume_tailor import checks, docxio, pdfx, rules
from tests.helpers import real_scores, example_profile

# a job that asks only for what the base already has, so the base passes the ATS checks against itself
BASE_JD = """Machine Learning Engineer\nRequirements\n- Python, SQL, PyTorch and scikit-learn\n- AWS, SageMaker, Docker, Kubernetes, MLflow and Airflow\n"""
JD_TEXT = BASE_JD

TOOLS = not pdfx.tools_missing()


class DocxRoundTrip(unittest.TestCase):
    def setUp(self):
        self.prof, self.base = example_profile()
        self.doc = docxio.read(self.base.docx)
        self.structure = docxio.structure(self.doc)

    def test_structure_reads_sections_and_counts(self):
        s = self.structure
        self.assertTrue(s.name)
        self.assertTrue(s.summary)
        self.assertGreaterEqual(len(s.skills), 4)
        self.assertEqual(len(s.jobs), 3)
        for job in s.jobs:
            self.assertTrue(job["company"] and job["dates"] and job["title"] and job["bullets"])
        self.assertFalse(any(sk.category.endswith(":") for sk in s.skills))

    def test_refill_with_own_content_changes_nothing(self):
        out = docxio.fill(self.doc, self.structure.as_content())
        other = docxio.read(out)
        self.assertEqual(docxio.format_diffs(self.doc, other), [])
        self.assertEqual(docxio.layout_diffs(self.structure, docxio.structure(other)), [])
        self.assertEqual(docxio.structure(other).as_content(), self.structure.as_content())

    def test_refill_changes_only_text_and_keeps_run_formatting(self):
        content = copy.deepcopy(self.structure.as_content())
        content["summary"] = "A new summary sentence for the test."
        content["jobs"][0]["bullets"][0] = "Built a thing that did a thing, measured by a number."
        content["skills"][0]["items"] = "Python, SQL"
        other = docxio.read(docxio.fill(self.doc, content))
        self.assertEqual(docxio.format_diffs(self.doc, other), [])
        s2 = docxio.structure(other)
        self.assertEqual(s2.summary, content["summary"])
        self.assertEqual(s2.jobs[0]["bullets"][0], content["jobs"][0]["bullets"][0])
        self.assertEqual(s2.skills[0].items, "Python, SQL")
        self.assertEqual(s2.jobs[0]["dates"], self.structure.jobs[0]["dates"])

    def test_wrong_counts_are_rejected(self):
        content = copy.deepcopy(self.structure.as_content())
        content["jobs"][0]["bullets"].append("one bullet too many")
        with self.assertRaises(docxio.DocxError):
            docxio.fill(self.doc, content)
        content = copy.deepcopy(self.structure.as_content())
        content["skills"].pop()
        with self.assertRaises(docxio.DocxError):
            docxio.fill(self.doc, content)

    def test_split_items_and_dated(self):
        self.assertEqual(docxio.split_items("A, B (x, y), C"), ["A", "B (x, y)", "C"])
        text, dates = docxio.split_dated("Acme, USA            Jan 2021 – Jul 2023")
        self.assertEqual((text, dates), ("Acme, USA", "Jan 2021 – Jul 2023"))


@unittest.skipUnless(TOOLS, "poppler tools missing")
class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof, cls.base = example_profile()
        cls.doc = docxio.read(cls.base.docx)
        cls.structure = docxio.structure(cls.doc)
        cls.measure = checks.measure(cls.base.pdf, cls.structure)
        cls.scores = real_scores(cls.prof, cls.base, cls.structure.full_text(),
                                 checks.era_texts(cls.structure, "alex-rivera", cls.prof.rules), jd=BASE_JD)

    def _input(self, docx_path=None, pdf_path=None, scores="real") -> checks.Input:
        return checks.Input("alex-rivera", self.structure, self.doc, self.measure, docx_path or self.base.docx,
                            pdf_path or self.base.pdf, JD_TEXT, self.prof.known_skills,
                            self.scores if scores == "real" else scores, self.base.id,
                            [b.text for b in self.prof.bases], self.prof.skill_text, self.prof.rules)

    def test_measure_describes_the_base(self):
        m = self.measure
        self.assertEqual(m.info.pages, 1)
        self.assertAlmostEqual(m.info.width, 612, delta=1)
        self.assertGreater(m.chars_per_line, 60)
        self.assertEqual(set(m.bullet_lines.values()), {2})
        self.assertTrue(m.skill_lines)

    def test_base_against_itself_passes_every_check(self):
        report = checks.run_all(self._input())
        self.assertEqual([f.message for f in report.failures], [])
        self.assertEqual(report.info["pages"], 1)
        self.assertIn("base_pct", report.info["ats"])

    def test_missing_scores_fail_check_6_only(self):
        report = checks.run_all(self._input(scores=None))
        self.assertEqual({f.check for f in report.failures}, {6})

    def test_check_writing_flags_banned_words_em_dashes_and_short_bullets(self):
        s = copy.deepcopy(self.structure)
        s.jobs[0]["bullets"][0] = "Spearheaded a robust pipeline — and leveraged cutting-edge tools — daily."
        s.jobs[0]["bullets"][1] = "Too short."
        fails = checks.check_writing(self._input(), s)
        text = " ".join(f.message for f in fails).lower()
        self.assertTrue(all(f.check == 7 for f in fails))
        for word in ("spearhead", "robust", "leverag", "cutting-edge", "em dash"):
            self.assertIn(word, text)

    def test_check_honesty_flags_new_numbers_and_unknown_tools(self):
        s = copy.deepcopy(self.structure)
        s.jobs[0]["bullets"][0] = "Cut costs by 93.7% for 8,421 users with Zorbotron and Kubeflow."
        scores = {"found": {"tailored": {"Zorbotron": 1, "Python": 2}, "known": {"Python": 1}, "ml_engineer": {}},
                  "categories": {}, "jd_found": {}}
        fails, info = checks.check_honesty(self._input(), s, scores)
        names = {f.name for f in fails}
        self.assertIn("numbers", names)
        self.assertIn("unknown_skills", names)
        self.assertIn("Zorbotron", info["unknown_terms"])
        self.assertTrue(any("93.7%" in b for b in info["bad_numbers"]))

    def test_honesty_allows_jd_skills_when_the_rules_say_so_but_enforces_era(self):
        s = copy.deepcopy(self.structure)
        inp = self._input()
        inp.profile_rules = {**self.prof.rules, "jd_skills_allowed": True}
        scores = {"found": {"tailored": {"Weaviate": 1, "Kubeflow": 1}, "known": {}, "ml_engineer": {},
                            "era_job_2": {"LangChain": 1}},
                  "categories": {"LangChain": "GenAI & Agentic AI"}, "jd_found": {"Kubeflow": 1, "Weaviate": 1}}
        fails, info = checks.check_honesty(inp, s, scores)
        self.assertEqual(info["added_from_jd"], ["Kubeflow", "Weaviate"])
        self.assertEqual(info["unknown_terms"], [])
        self.assertEqual({f.name for f in fails}, {"era"})
        self.assertIn("LangChain", info["era_violations"])

    def test_check_ats_fails_when_tailored_drops_below_base(self):
        scores = {"scores": [{"id": "ml_engineer", "pct": 70, "missing": []},
                             {"id": "tailored", "pct": 60, "missing": ["gRPC"], "matched": []}],
                  "found": {"known": {"Airflow": 1}, "tailored": {}}, "terms": [{"name": "Airflow", "need": True}],
                  "vendor_label": "x", "label": "y"}
        fails, info = checks.check_ats(self._input(), scores)
        self.assertEqual({f.name for f in fails}, {"ats_score", "ats_coverage"})
        self.assertEqual(info["uncovered_known"], ["Airflow"])

    def test_two_page_pdf_fails_check_1(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            two = tmp / "two.pdf"
            two.write_bytes(_two_page_pdf())
            fails = checks.check_page(self._input(pdf_path=two), pdfx.info(two), self.doc)
            self.assertTrue(any(f.check == 1 and "2 page" in f.message for f in fails))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fit_passes_for_the_base_and_threshold_is_configured(self):
        lines = pdfx.flat_lines(pdfx.lines(self.base.pdf))
        fails, info = checks.check_fit(self._input(), copy.deepcopy(self.structure), lines)
        self.assertEqual(fails, [])
        self.assertEqual(info["widows"], [])
        self.assertGreater(rules.widow_share(), 0.1)


def _two_page_pdf() -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


if __name__ == "__main__":
    unittest.main()


class DateLineTests(unittest.TestCase):
    """Google Docs re-flows the base's typed-space date padding: 'Jul 2025 –' on one line, 'Jun 2026' on the next."""

    def _lines(self, *texts):
        return [pdfx.Line(t, 0, i * 10.0, 100, i * 10.0 + 8) for i, t in enumerate(texts)]

    def _structure(self):
        prof, base = example_profile()
        return base.structure

    @unittest.skipUnless(TOOLS, "poppler tools missing")
    def test_base_pdf_keeps_every_date_on_one_line(self):
        prof, base = example_profile()
        lines = pdfx.flat_lines(pdfx.lines(base.pdf))
        self.assertEqual(checks.date_problems(lines, base.structure), [])

    def test_wrapped_date_is_reported(self):
        s = self._structure()
        dates = s.jobs[1]["dates"]
        first, second = dates.split("–")[0].strip() + " –", dates.split("–")[1].strip()
        lines = self._lines("Lumen Freight, USA " + s.jobs[0]["dates"], "Brightline Health, USA " + first, second,
                            "Northwind Analytics, USA " + s.jobs[2]["dates"],
                            *(e.replace(" Sep 2016", "  Sep 2016") for e in s.education))
        problems = checks.date_problems(lines, s)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(dates, problems[0])
        self.assertIn("Brightline Health", problems[0])
