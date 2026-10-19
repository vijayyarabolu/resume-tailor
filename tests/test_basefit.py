"""The real-margin check (page box from pgMar) and the base fit (date padding trim)."""
import copy
import unittest

from resume_tailor import basefit, checks, docxio, pdfx, pipeline
import math

from tests.helpers import example_profile, padded_doc

TOOLS = not pdfx.tools_missing()


@unittest.skipUnless(TOOLS, "poppler tools missing")
class RealMarginTests(unittest.TestCase):
    def setUp(self):
        self.prof, self.base = example_profile()
        self.doc = docxio.read(self.base.docx)
        self.measure = pipeline.measure_base(self.base)

    def _input(self, measure):
        return checks.Input("alex-rivera", self.base.structure, self.doc, measure, self.base.docx, self.base.pdf, "jd",
                            self.prof.known_skills, None, self.base.id, [], "", {})

    def test_page_box_comes_from_the_section_margins(self):
        left, top, right, bottom = docxio.page_box(self.doc)
        self.assertEqual((left, top, right, bottom), (43.2, 39.6, 568.8, 752.4))     # 0.6" sides, 0.55" top/bottom
        self.assertEqual(self.measure.margins, (left, top, right, bottom))
        self.assertGreater(docxio.para_right_edge(self.doc, self.doc.paras[0].el), 500)

    def test_text_inside_the_page_but_past_the_widest_base_line_passes(self):
        lines = pdfx.flat_lines(pdfx.lines(self.base.pdf))
        narrow = copy.copy(self.measure)
        narrow.ink = (24, 9, 500, 771)                          # a base whose widest line ends well before ours
        narrow.column_right = 500.0
        fails, info = checks.check_visual(self._input(narrow), self.base.pdf, lines)
        self.assertEqual([f.name for f in fails], [])
        self.assertTrue(info["protruding"])                     # ... but the lines are reported as protruding

    def test_text_past_the_real_margin_fails(self):
        tight = copy.copy(self.measure)
        tight.margins = (18.0, 0.0, 560.0, 600.0)
        fails, _ = checks.check_visual(self._input(tight), self.base.pdf, pdfx.flat_lines(pdfx.lines(self.base.pdf)))
        self.assertEqual({f.name for f in fails}, {"right_margin", "bottom_margin"})
        self.assertIn("page's right margin", next(f.message for f in fails if f.name == "right_margin"))

    def test_overflow_lines_counts_the_excess(self):
        info = pdfx.info(self.base.pdf)
        inp = self._input(self.measure)
        self.assertEqual(checks.overflow_lines(inp, self.base.pdf, info, (43, 43, 568, 700)), 0)
        want = math.ceil(30 / self.measure.line_height)
        self.assertEqual(checks.overflow_lines(inp, self.base.pdf, info, (43, 43, 568, 752.4 + 30)), want)


class PaddingTrimTests(unittest.TestCase):
    def setUp(self):
        self.prof, self.base = example_profile()
        self.doc = padded_doc(docxio.read(self.base.docx))
        self.header = next(p for p in self.doc.paras if p.kind == "job_header" and docxio.padding_runs(p.el))

    def test_trim_keeps_text_and_formatting(self):
        content = self.base.structure.as_content()
        out = docxio.read(docxio.fill(self.doc, content, pad_trim={self.header.index: 20}))
        self.assertEqual(docxio.format_diffs(self.doc, out), [])
        self.assertEqual(docxio.layout_diffs(self.base.structure, docxio.structure(out)), [])
        before = sum(len(docxio.run_text(r)) for r in docxio.padding_runs(self.header.el))
        after = sum(len(docxio.run_text(r)) for r in docxio.padding_runs(out.paras[self.header.index].el))
        self.assertEqual(before - after, 20)

    def test_trim_never_goes_below_the_minimum(self):
        p = copy.deepcopy(self.header.el)
        total = sum(len(docxio.run_text(r)) for r in docxio.padding_runs(p))
        removed = docxio.trim_padding(p, 10_000)
        self.assertEqual(removed, total - docxio.MIN_PADDING * len(docxio.padding_runs(p)))

    def test_estimate_from_wrapped_geometry(self):
        job = next(j for j in self.base.structure.jobs if j["header_para"] == self.header.index)
        first, second = job["dates"].split("–")
        lines = [pdfx.Line(job["company"], 43.0, 430.0, 139.0, 440.0),
                 pdfx.Line(first.strip() + " –", 520.0, 430.0, 565.0, 440.0),
                 pdfx.Line(second.strip(), 43.0, 444.0, 89.0, 454.0)]
        est = basefit.estimate_trims(self.doc, self.base.structure, [lines])
        self.assertEqual(list(est), [self.header.index])
        self.assertGreaterEqual(est[self.header.index], 20)
        self.assertLessEqual(est[self.header.index], 30)

    def test_no_google_means_no_trim(self):
        fit = basefit.fit(self.base, self.doc, None, "google")
        self.assertEqual((fit.trim, fit.pdf), ({}, None))


if __name__ == "__main__":
    unittest.main()
