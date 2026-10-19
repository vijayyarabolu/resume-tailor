"""Base fit: make each PDF engine draw the base the way its author meant, once per base.

Resumes often push "Company, Country" and "Mon YYYY – Mon YYYY" apart with a long run of typed spaces whose run
carries a negative character spacing. Word honours that spacing; Google Docs and sometimes LibreOffice do not, the
line grows and the date wraps ("Jun 2026" alone on the next line, or the education date on page 2). The fix lives
in the .docx we send: trim the padding spaces of each header whose date wrapped, by an amount estimated from the
rendered geometry, then verify by rendering again (at most MAX_ROUNDS renders, once per base, cached under
<home>/basefit/). A base laid out with a right tab stop needs no trim at all.

fit(base, doc, google, engine) -> Fit(trim={paragraph index: spaces removed}, pdf=the base rendered that way).
The measured line budget (checks.measure) is then taken from that PDF, so every budget matches the engine in use.
"""
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from . import checks, config, docxio, pdfx
from . import render
from .gdocs import GoogleError

log = logging.getLogger(__name__)
MAX_ROUNDS = 5
SPACE_PT = 2.3             # one padding space is about 2.4 pt in a 10 to 11 pt serif (2.0 left dates 15 px short)
EXTRA_SPACES = 1           # space or two at most, which only moves a date a hair to the left


class FitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fit:
    engine: str
    trim: dict                      # {paragraph index (int): spaces removed}
    pdf: Path | None                # the base rendered by `engine` with the trim; None = use the base's own PDF
    rounds: int = 0
    problems: tuple = ()            # date problems still present after MAX_ROUNDS (jobs will report them)

    def as_dict(self) -> dict:
        return {"engine": self.engine, "trim": {str(k): v for k, v in self.trim.items()},
                "pdf": str(self.pdf) if self.pdf else None, "rounds": self.rounds, "problems": list(self.problems)}


def cache_dir() -> Path:
    return config.DATA / "basefit"


def _dated_paragraphs(doc: docxio.Doc, structure: docxio.Structure) -> list:
    """(paragraph index, lead text, date text) for every job header and dated education line."""
    out = [(job["header_para"], job["company"], job["dates"]) for job in structure.jobs if job.get("dates")]
    for para in doc.paras:
        if para.kind != "edu":
            continue
        for m in checks._DATE_RE.finditer(para.text):
            lead = para.text[:m.start()].strip()
            out.append((para.index, lead, m.group(0)))
    return out


def estimate_trims(doc: docxio.Doc, structure: docxio.Structure, lines_by_page: list) -> dict:
    """{paragraph index: spaces to remove} for each header whose date is not on the line of its lead text."""
    rows = checks.physical_rows(lines_by_page)
    squashed = [checks.squash(r["text"]) for r in rows]
    paras = {p.index: p.el for p in doc.paras}
    out = {}
    for idx, lead, dates in _dated_paragraphs(doc, structure):
        want_lead, want_date = checks.squash(lead), checks.squash(dates)
        if not want_lead or not want_date:
            continue
        start = next((i for i, s in enumerate(squashed) if s.startswith(want_lead)), None)
        if start is None or want_date in squashed[start]:
            continue                                       # not found, or already on one line
        remainder, acc = 0.0, squashed[start]
        for row, s in zip(rows[start + 1:start + 4], squashed[start + 1:start + 4]):
            acc += s
            remainder += row["width"] + SPACE_PT
            if acc.endswith(want_date):
                break
        right = docxio.para_right_edge(doc, paras[idx])
        overflow = rows[start]["x1"] + remainder - right
        spaces = max(1, math.ceil(max(overflow, SPACE_PT) / SPACE_PT)) + EXTRA_SPACES
        out[idx] = spaces
    return out


def _lines_by_page(pdf: Path) -> list:
    pages = max(1, pdfx.info(pdf).pages)
    return [pdfx.flat_lines(pdfx.lines(pdf, page)) for page in range(1, pages + 1)]


def _load(path: Path) -> Fit | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pdf = Path(data["pdf"]) if data.get("pdf") else None
    if pdf is None or not pdf.exists():
        return None
    return Fit(str(data.get("engine")), {int(k): int(v) for k, v in (data.get("trim") or {}).items()}, pdf,
               int(data.get("rounds") or 0), tuple(data.get("problems") or ()))


def fit(base, doc: docxio.Doc, google, engine: str, force: bool = False) -> Fit:
    """The trim that keeps every date on one line through this engine (cached per base content). Word with a base
    that has its own PDF: that PDF is the measure and nothing is trimmed. Google without a connection: the same."""
    if engine == "word" and base.pdf is not None:
        return Fit(engine, {}, None)
    if engine == "google" and google is None:
        return Fit(engine, {}, None)
    folder = cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    meta, pdf_path = folder / f"{base.sha[:16]}-{engine}.json", folder / f"{base.sha[:16]}-{engine}.pdf"
    cached = None if force else _load(meta)
    if cached is not None:
        return cached

    def draw(trim: dict) -> bytes:
        data = docxio.fill(doc, content, pad_trim=trim)
        try:
            if engine == "google":
                return google.docx_to_pdf(data, f"basefit {base.id}")
            return render.render_with(engine, data, folder, f"basefit {base.id}")
        except (GoogleError, render.RenderError) as err:
            raise FitError(f"{engine} could not render the base {base.label}: {err}") from err

    structure = base.structure
    content = structure.as_content()
    trim, problems = {}, ()
    rounds = 1 if engine == "word" else MAX_ROUNDS
    for round_no in range(1, rounds + 1):
        pdf_path.write_bytes(draw(trim))
        by_page = _lines_by_page(pdf_path)
        problems = tuple(checks.date_problems([], structure, by_page=by_page))
        if not problems:
            result = Fit(engine, dict(trim), pdf_path, round_no)
            meta.write_text(json.dumps(result.as_dict(), indent=1))
            log.info("base fit %s (%s): %s round(s), trim %s", base.id, engine, round_no, trim)
            return result
        more = estimate_trims(doc, structure, by_page) if engine != "word" else {}
        if not more:
            break
        trim = {**trim, **{k: trim.get(k, 0) + v for k, v in more.items()}}
    result = Fit(engine, dict(trim), pdf_path, rounds, problems)
    meta.write_text(json.dumps(result.as_dict(), indent=1))
    log.warning("base fit %s (%s): dates still wrap: %s", base.id, engine, problems)
    return result
