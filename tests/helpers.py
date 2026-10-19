"""Shared test helpers. Every test runs against a temporary home folder (the real ~/.resume-tailor is never
touched) holding the fictional Alex Rivera profile and a PDF of its base resume rendered once by LibreOffice
(tests/fixtures), so no test needs LibreOffice, Word, Google or the network."""
import atexit
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path

import xml.etree.ElementTree as ET

from resume_tailor import ats, config, docxio, profiles

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _chrome() -> str:
    """A Chrome or Chromium for the browser tests: CHROME_PATH, Google Chrome on a Mac, Playwright's Chromium, or one
    on PATH. "" when there is none (those tests skip)."""
    candidates = [os.environ.get("CHROME_PATH") or "", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    candidates += sorted(str(p) for p in Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/pw-browsers")
                         .glob("chromium-*/chrome-linux/chrome"))
    candidates += [shutil.which(n) or "" for n in ("chromium", "chromium-browser", "google-chrome")]
    return next((c for c in candidates if c and Path(c).exists()), "")


CHROME = _chrome()
PID = "alex-rivera"
BASE_ID = "ml_engineer"
BASE_STEM = "Alex_Rivera_ML_Engineer"
HOME_CITY = "Portland, OR"
DEFAULT_SETTINGS = {"pdf_engine": "word", "claude_backend": "api", "max_claude_calls": 0}

# Every required skill here is already on the example base, so the base's own content passes the ATS checks;
# the nice-to-have ones are skills Alex lists in known_skills.txt.
JD_TEXT = """Senior Machine Learning Engineer
Remote (US)
About Acme
Acme builds machine learning platforms for logistics customers. We are hiring a Senior Machine Learning
Engineer to own model training, deployment and monitoring in production.
What you will do
- Build and operate ML pipelines with Python, PyTorch and Airflow on AWS (SageMaker).
- Serve models on Kubernetes with CI/CD and Docker; track experiments in MLflow.
- Run A/B tests and shadow deployments with the product team.
Requirements
- 4+ years with Python and SQL in production.
- Hands-on with LLMs and RAG for internal tools.
Nice to have
- Snowflake, Redshift or Databricks.
- Datadog, Jenkins and feature stores.
"""


def install_profiles(names=(PID,)) -> None:
    """The example profile (and, for two-person tests, a copy named Sam Lee) with the fixture PDF beside the
    base, so the "word" engine measures that PDF and nothing renders."""
    config.ensure_dirs()
    for pid in names:
        target = config.PROFILES / pid
        if target.exists():
            continue
        shutil.copytree(config.EXAMPLES / PID, target)
        shutil.copy(FIXTURES / f"{BASE_STEM}.pdf", target / "resumes" / f"{BASE_STEM}.pdf")
        if pid != PID:
            (target / "profile.toml").write_text((target / "profile.toml").read_text()
                                                 .replace('name = "Alex Rivera"', 'name = "Sam Lee"'))
    profiles.reload()


os.environ.pop("RESUME_TAILOR_DEMO", None)
_SESSION = Path(tempfile.mkdtemp(prefix="resume-tailor-tests-"))
atexit.register(shutil.rmtree, _SESSION, True)
config.set_home(_SESSION)
config.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
config.SETTINGS_PATH.write_text(json.dumps(DEFAULT_SETTINGS))
install_profiles((PID, "sam-lee"))


class TempEnv:
    """A fresh home folder for one test, with the example profile installed. `people` adds more profiles
    (copies of the example under other ids)."""

    def __init__(self, settings: dict | None = None, people=(PID, "sam-lee")):
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.people = people
        self.saved = None
        self.tmp = None

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-tailor-test-"))
        self.saved = config.DATA
        config.set_home(self.tmp / "home")
        config.ensure_dirs()
        config.SETTINGS_PATH.write_text(json.dumps(self.settings))
        install_profiles(self.people)
        return self

    def __exit__(self, *exc):
        config.set_home(self.saved)
        profiles.reload()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def add_second_base(pid: str = PID, headline: str = "Data Scientist") -> str:
    """A second base resume for the profile (the example's with another title), for tests that pick between bases.
    Returns its id."""
    folder = config.PROFILES / pid / "resumes"
    src = folder / f"{BASE_STEM}.docx"
    struct = docxio.structure(docxio.read(src))
    content = {**struct.as_content(), "headline": headline}
    stem = f"Alex_Rivera_{headline.replace(' ', '_')}"
    (folder / f"{stem}.docx").write_bytes(docxio.fill(docxio.read(src), content))
    shutil.copy(FIXTURES / f"{BASE_STEM}.pdf", folder / f"{stem}.pdf")
    profiles.reload()
    return headline.lower().replace(" ", "_")


def example_profile():
    prof = profiles.load(PID)
    base = prof.base(BASE_ID)
    assert base is not None, "the example ML Engineer base is missing"
    return prof, base


def real_scores(prof, base, tailored_text: str, era: dict | None = None, jd: str = JD_TEXT):
    """The keyword model's scores for the bases + a 'tailored' text."""
    entries = [{"id": b.id, "label": b.label, "text": b.text, "families": list(b.families)} for b in prof.bases]
    entries.append({"id": "tailored", "label": "Tailored", "text": tailored_text, "families": []})
    return ats.score(jd, entries, title="Senior Machine Learning Engineer", company="Acme", jd_url=None,
                     profile_years=prof.years, extra_texts={"known": prof.known_text(), **(era or {})})


def padded_doc(doc: docxio.Doc, spaces: int = 120) -> docxio.Doc:
    """The base with each dated line's tab replaced by a run of typed spaces, the way many hand-made resumes push
    dates to the right (the layout the base fit and the padding trim exist for)."""
    d = copy.deepcopy(doc)
    w = docxio.W
    for para in d.paras:
        if para.kind not in ("job_header", "edu"):
            continue
        for r in docxio.runs_of(para.el):
            tab = r.find(w + "tab")
            if tab is None:
                continue
            r.remove(tab)
            pad = copy.deepcopy(r)
            for t in pad.findall(w + "t"):
                pad.remove(t)
            t = ET.SubElement(pad, w + "t")
            t.text = " " * spaces
            t.set(docxio.XML_SPACE, "preserve")
            para.el.insert(list(para.el).index(r), pad)
    return docxio.read(docxio.serialize(d))
