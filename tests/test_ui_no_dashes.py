"""No em or en dashes in what the app shows (they read as machine-written; a tracker writes "Company · Role").
Card titles, the panel title, toasts and event lines say "Company · Role"; the Outdated badge says "Outdated: why";
a check's rule says "15 to 25"."""
import re
import unittest
from pathlib import Path

from resume_tailor import outdated

STATIC = Path(__file__).resolve().parent.parent / "resume_tailor" / "static"
DASH = re.compile("[–—]")


class NoDashesTest(unittest.TestCase):
    def test_the_page_script_writes_no_dash(self):
        for n, line in enumerate((STATIC / "app.js").read_text().splitlines(), 1):
            code = line.split("//")[0] if not line.lstrip().startswith("//") else ""
            with self.subTest(line=n):
                self.assertNotRegex(code, DASH)

    def test_the_outdated_badge(self):
        self.assertEqual(outdated.badge([outdated.OLD_BASE]), "Outdated: made from your old resume")
        self.assertEqual(outdated.badge(["date wraps: May 2025 (Education)"]), "Outdated: date wraps: May 2025 (Education)")

    def test_messages_written_for_the_page(self):
        for name in ("checks.py", "db.py", "pipeline.py"):
            src = (STATIC.parent / name).read_text()
            for m in re.finditer(r'f"[^"\n]*"', src):
                with self.subTest(file=name, text=m.group(0)[:60]):
                    if "prompt" not in m.group(0).lower():
                        self.assertNotRegex(m.group(0), DASH)
