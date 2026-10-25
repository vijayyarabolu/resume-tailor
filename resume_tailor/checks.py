"""The eight strict checks, run on the real .docx and PDF of a tailored resume against its base.

run_all(inp) -> Report(failures, info). Every failure names the check and says exactly what is wrong, in words a
retry prompt can act on ("bullet 4 of role 2 is a widow", "'Kubernetes' not in known skills").
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import confirmed, docxio, pdfx, rules

INK = 160
MARGIN_TOL_PX = 4             # at 72 dpi: 4 px = 0.055 in
BAND_TOL_PX = 3
PAGE_TOL_PT = 1.0
NUMBER = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
_LIG = {"ﬁ": "fi", "ﬂ": "fl", "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"',
        "”": '"'}


@dataclass(frozen=True)
class Failure:
    check: int
    name: str
    message: str

    def as_dict(self) -> dict:
        return {"check": self.check, "name": self.name, "message": self.message}


@dataclass
class Report:
    failures: list = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {"ok": self.ok, "failures": [f.as_dict() for f in self.failures], "info": self.info}

    @classmethod
    def from_dict(cls, data: dict) -> "Report":
        failures = [Failure(int(f["check"]), str(f["name"]), str(f["message"])) for f in data.get("failures") or []]
        return cls(failures, dict(data.get("info") or {}))


@dataclass
class Measure:
    """What the base PDF looks like (computed once per base)."""
    info: pdfx.Info
    fonts: set
    lines: list
    skill_lines: dict                 # category -> rendered lines
    skill_total: int
    column_left: float
    column_right: float
    bullet_lines: dict                # (job index, bullet index) -> line count
    ink: tuple
    band_max: int
    chars_per_line: float
    bullets_end_with_period: bool
    text_fonts: set = field(default_factory=set)      # families carrying visible text (pdfx.text_fonts)
    margins: tuple = (0.0, 0.0, 612.0, 792.0)         # real page box (left, top, right, bottom) from pgSz/pgMar
    line_height: float = 12.0                         # typical rendered line pitch (points)
    summary_lines: int = 0                            # rendered lines of the base's summary


@dataclass
class Input:
    pid: str
    base_structure: docxio.Structure
    base_doc: docxio.Doc
    base_measure: Measure
    docx_path: Path
    pdf_path: Path
    jd_text: str
    known_skills: list
    scores: dict | None               # ats.score() result (ids: every base + "tailored"; extras known, jd)
    base_id: str
    all_base_texts: list              # full texts of every base of this person
    skill_text: str = ""              # the person's SKILL.md (its FACT BANK numbers count as base facts)
    profile_rules: dict = field(default_factory=dict)   # the parsed rules.toml: bullet_words, summary_words, ...
    role: str = ""                    # the posting's title (the title under the name follows it)
    location: str = ""                # the job's "City, ST" for the contact line ("" = the person's own)
    notes: tuple = ()                 # this job's Fix-with-note texts (what they name is user-confirmed)
    fact_bank: str = ""               # facts.md above "## Part 2" (fact_bank_numbers: numbers from here and this base)
    mirror: dict = field(default_factory=dict)   # the answer's mirrored / jd_domain (tailoring rounds only)
    fact_bank_all: str = ""           # the whole facts.md (Part 2 shapes wording, never new numbers)
    answer: dict = field(default_factory=dict)   # the answer's swaps / skills_added / skills_removed (tailorchecks)


def job_rules(inp: "Input") -> dict:
    """The person's rules for this job: with its headline and summary opening (rules.for_job)."""
    prules = rules.for_profile(inp.pid, inp.profile_rules)
    return rules.for_job(prules, getattr(inp, "role", ""), inp.base_structure.headline,
                         getattr(inp, "location", "") or None)


# ---------- text helpers ----------

def squash(text: str) -> str:
    for k, v in _LIG.items():
        text = text.replace(k, v)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _is_glyph_line(line: pdfx.Line) -> bool:
    return squash(line.text) == ""


def locate(lines: list, text: str, start: int = 0):
    """(first, last) indexes of the consecutive PDF lines that spell `text` (ignoring spaces, case, dashes)."""
    want = squash(text)
    if not want:
        return None
    for i in range(start, len(lines)):
        if _is_glyph_line(lines[i]):
            continue
        acc = ""
        for j in range(i, min(len(lines), i + 12)):
            if _is_glyph_line(lines[j]):
                continue
            acc += squash(lines[j].text)
            if acc == want:
                return i, j
            if not want.startswith(acc):
                break
    return None


def section_lines(lines: list, heading: str, next_headings: tuple) -> list:
    inside, out = False, []
    stops = {squash(h) for h in next_headings}
    for ln in lines:
        key = squash(ln.text)
        if key == squash(heading):
            inside = True
            continue
        if inside and key in stops:
            break
        if inside:
            out.append(ln)
    return out


def skill_line_counts(lines: list, structure: docxio.Structure) -> dict:
    """Rendered lines per skill category, scanning the Skills section top to bottom."""
    others = tuple(h for h in structure.sections if h.lower() != "skills")
    section = section_lines(lines, "Skills", others)
    counts, current = {}, None
    starts = {squash(s.category): s.category for s in structure.skills}
    for ln in section:
        key = squash(ln.text)
        hit = next((cat for sq, cat in starts.items() if sq and key.startswith(sq)), None)
        if hit is not None and hit != current:
            current = hit
        if current is not None:
            counts[current] = counts.get(current, 0) + 1
    return counts


def skill_row_texts(lines: list, structure: docxio.Structure) -> dict:
    """The rendered line texts of each skill row ({category: [line, ...]}), scanning the Skills section."""
    others = tuple(h for h in structure.sections if h.lower() != "skills")
    rows, current = {}, None
    starts = {squash(s.category): s.category for s in structure.skills}
    for ln in section_lines(lines, "Skills", others):
        key = squash(ln.text)
        hit = next((cat for sq, cat in starts.items() if sq and key.startswith(sq)), None)
        current = hit if hit is not None else current
        if current is not None:
            rows.setdefault(current, []).append(ln.text)
    return rows


def rendered_lines(lines: list, text: str) -> int:
    """How many PDF lines `text` occupies (0 when it cannot be located)."""
    found = locate(lines, text)
    if found is None:
        return 0
    return len([ln for ln in lines[found[0]:found[1] + 1] if not _is_glyph_line(ln)])


def _line_pitch(lines: list) -> float:
    tops = sorted(ln.y0 for ln in lines)
    gaps = [b - a for a, b in zip(tops, tops[1:]) if 6.0 <= b - a <= 30.0]
    return sorted(gaps)[len(gaps) // 2] if gaps else 12.0


def measure(pdf: Path, structure: docxio.Structure, doc: docxio.Doc | None = None) -> Measure:
    """What the base looks like rendered by the PDF engine in use. `doc` (the base .docx) supplies the real page
    box; without it the page size stands in for the margins."""
    info = pdfx.info(pdf)
    fonts = pdfx.fonts(pdf)
    lines = pdfx.flat_lines(pdfx.lines(pdf))
    counts = skill_line_counts(lines, structure)
    bullet_lines, pos, lefts, rights, chars = {}, 0, [], [], []
    for j, job in enumerate(structure.jobs):
        for k, text in enumerate(job["bullets"]):
            found = locate(lines, text, pos)
            if found is None:
                continue
            first, last = found
            span = [ln for ln in lines[first:last + 1] if not _is_glyph_line(ln)]
            bullet_lines[(j, k)] = len(span)
            lefts += [ln.x0 for ln in span]
            rights += [ln.x1 for ln in span]
            chars += [len(ln.text) for ln in span[:-1]]      # full lines only
            pos = last + 1
    w, h, rows = pdfx.raster(pdf, 72)
    ink = pdfx.ink_bbox(w, h, rows) or (0, 0, w, h)
    bands = pdfx.bands(w, h, rows)
    ends = [b.rstrip().endswith(".") for job in structure.jobs for b in job["bullets"]]
    margins = docxio.page_box(doc) if doc is not None else (0.0, 0.0, info.width, info.height)
    return Measure(info, fonts, lines, counts, sum(counts.values()), min(lefts) if lefts else 0.0,
                   max(rights) if rights else info.width, bullet_lines, ink, max((b.height for b in bands), default=0),
                   (sum(chars) / len(chars)) if chars else 110.0, sum(ends) >= 0.8 * max(1, len(ends)),
                   pdfx.text_fonts(pdf), margins, _line_pitch(lines), rendered_lines(lines, structure.summary))


# ---------- the checks ----------

def check_page(inp: Input, info: pdfx.Info, tdoc: docxio.Doc) -> list:
    out = []
    base = inp.base_measure.info
    if info.pages != 1:
        out.append(Failure(1, "pages", f"the PDF has {info.pages} pages; it must be exactly 1 (shorten the longest "
                                       f"bullets or the summary, or drop a skill from a 2-line category)"))
    if abs(info.width - base.width) > PAGE_TOL_PT or abs(info.height - base.height) > PAGE_TOL_PT:
        out.append(Failure(1, "page_size", f"page size {info.width:.0f}x{info.height:.0f} pt (base "
                                           f"{base.width:.0f}x{base.height:.0f}, US Letter)"))
    sect_a = docxio._canon(inp.base_doc.root.find(f"{docxio.W}body/{docxio.W}sectPr"))
    sect_b = docxio._canon(tdoc.root.find(f"{docxio.W}body/{docxio.W}sectPr"))
    if sect_a != sect_b:
        out.append(Failure(1, "margins", "page margins or section settings differ from the base .docx"))
    return out


# an education date: a range ("Aug 2023 – May 2025") or one date ("May 2025")
_DATE_RE = re.compile(r"[A-Z][a-z]{2} \d{4}(?:\s*[–-]\s*(?:[A-Z][a-z]{2} \d{4}|Present))?")


ROW_TOL_PT = 2.0          # boxes whose tops are this close share one printed row
LEAD_TAIL = 16            # characters of the left text that must share the date's row


def physical_rows(lines_by_page: list) -> list:
    """pdftotext splits one printed line into several boxes when the gap is wide (the date padding) or the font
    changes. Boxes that share a baseline become one row {text (x order), x1, width, y}, page after page."""
    rows = []
    for page, lines in enumerate(lines_by_page):
        for ln in sorted(lines, key=lambda ln: ln.y0):
            y = ln.y0 + page * 10_000
            row = next((r for r in rows if abs(r["y"] - y) <= ROW_TOL_PT), None)
            if row is None:
                rows.append({"y": y, "boxes": [ln]})
            else:
                row["boxes"].append(ln)
    rows.sort(key=lambda r: r["y"])
    out = []
    for r in rows:
        boxes = sorted(r["boxes"], key=lambda ln: ln.x0)
        out.append({"text": " ".join(b.text for b in boxes), "x1": max(b.x1 for b in boxes),
                    "width": sum(b.x1 - b.x0 for b in boxes), "y": r["y"]})
    return out


def dated_lines(structure: docxio.Structure) -> list:
    """(owner, left text, date) for every role header and every dated education line."""
    out = [(job["company"], job["company"], job["dates"]) for job in structure.jobs if job.get("dates")]
    for text in structure.education:
        for m in _DATE_RE.finditer(text):
            out.append(("Education", text[:m.start()].strip(), m.group(0)))
    return out


def date_problems(lines: list, structure: docxio.Structure, by_page: list | None = None) -> list:
    """Every right-aligned date (each role's and the education line's) must print on ONE row, the same row as its
    left text: Google Docs re-flows the base's typed-space padding and either splits the date ('Jun 2026' alone
    on the next line) or drops the whole date below its line ('Aug 2023 – May 2025' under the degree),
    which no text check sees."""
    rows = physical_rows(by_page if by_page is not None else [lines])
    squashed = [squash(r["text"]) for r in rows]
    out = []
    for owner, lead, dates in dated_lines(structure):
        key, tail = squash(dates), squash(lead)[-LEAD_TAIL:]
        if not key:
            continue
        hits = [s for s in squashed if key in s]
        if not hits:
            out.append(f"the date '{dates}' of {owner} wraps onto two lines in the PDF (the renderer re-flowed the "
                       f"header line); the text is right, the PDF engine is not")
        elif tail and not any(tail in s for s in hits):
            out.append(f"the date '{dates}' of {owner} wraps onto the next line in the PDF (it prints below its "
                       f"line instead of beside it); the text is right, the PDF engine is not")
    return out


def skill_item_problems(base: docxio.Structure, tstruct: docxio.Structure) -> list:
    """skill_rows_fixed: every base item stays in its row (reorder only; an allowed JD skill may join). A dropped or
    renamed item ('Multi-Agent Workflows' -> 'Multi-Agent Orchestration') fails."""
    out = []
    for master, row in zip(base.skills, tstruct.skills):
        have = {squash(i) for i in docxio.split_items(row.items)}
        lost = [i.strip() for i in docxio.split_items(master.items) if squash(i) not in have]
        if lost:
            out.append(Failure(2, "skill_items", f"skills '{master.category}' dropped base items: {', '.join(lost)}; "
                                                 "keep every base item in its row and only reorder them"))
    return out


def check_layout(inp: Input, tstruct: docxio.Structure, lines: list | None = None) -> list:
    prules = job_rules(inp)
    want = prules.get("headline")
    may = prules["headline_may_change"] or bool(want)
    out = [Failure(2, "layout", m) for m in docxio.layout_diffs(inp.base_structure, tstruct, may,
                                                                 rules.contact_location(prules))]
    if want and tstruct.headline != want:                     # the job family's title, set by Python
        out.append(Failure(2, "layout", f"headline is {tstruct.headline!r}; the title under the name must be "
                                        f"{want!r} (the plain title for this posting's family)"))
    if prules.get("skill_rows_fixed"):                        # row names and order are the base's
        got, base = [s.category for s in tstruct.skills], [s.category for s in inp.base_structure.skills]
        if got != base:
            out.append(Failure(2, "skill_rows", f"skill rows are {got}; they must stay the base's, in order: {base}"))
        if not prules.get("skills_add_first"):              # with skills_add_first the [drop_order] decides removals
            out += skill_item_problems(inp.base_structure, tstruct)
    if lines:
        out += [Failure(2, "date_wrap", m) for m in date_problems(lines, tstruct)]
    return out


BULLET_FONTS = {"arial", "symbol", "notosanssymbols", "segoeuisymbol", "liberationsans"}   # the ● glyph only


def check_fonts(inp: Input, tdoc: docxio.Doc, pdf: Path) -> list:
    out = [Failure(3, "format", m) for m in docxio.format_diffs(inp.base_doc, tdoc)]
    base_text = inp.base_measure.text_fonts - BULLET_FONTS
    got = pdfx.text_fonts(pdf) - BULLET_FONTS
    extra = got - base_text
    if extra:
        out.append(Failure(3, "pdf_fonts", f"the PDF's text uses fonts the base never had: {sorted(extra)} "
                                           f"(base: {sorted(base_text)})"))
    return out


def check_fit(inp: Input, tstruct: docxio.Structure, lines: list) -> tuple:
    out, info = [], {"widows": [], "bullet_lines": {}, "skill_lines": {}}
    m = inp.base_measure
    prules = job_rules(inp)
    share = rules.widow_share()
    column = m.column_right - m.column_left
    pos = 0
    for j, job in enumerate(tstruct.jobs):
        for k, text in enumerate(job["bullets"]):
            found = locate(lines, text, pos)
            if found is None:
                out.append(Failure(4, "locate", f"bullet {k + 1} of role {j + 1} ({job['company']}) could not be "
                                                 f"found in the PDF text"))
                continue
            first, last = found
            span = [ln for ln in lines[first:last + 1] if not _is_glyph_line(ln)]
            pos = last + 1
            n = len(span)
            info["bullet_lines"][f"{j + 1}.{k + 1}"] = n
            last_width = span[-1].x1 - span[-1].x0
            if n > 1 and last_width < share * column:
                pct = int(100 * last_width / column)
                info["widows"].append(f"{j + 1}.{k + 1}")
                out.append(Failure(4, "widow", f"bullet {k + 1} of role {j + 1} ({job['company']}) is a widow: its "
                                               f"last line fills {pct}% of the width (minimum {int(100 * share)}%); "
                                               f"add or remove a few words"))
            if n < prules["min_bullet_lines"]:
                out.append(Failure(4, "short_bullet", f"bullet {k + 1} of role {j + 1} ({job['company']}) renders "
                                                      f"as {n} line; every bullet must fill "
                                                      f"{prules['min_bullet_lines']} lines"))
    counts = skill_line_counts(lines, tstruct)
    info["skill_lines"] = counts
    info["summary_lines"] = rendered_lines(lines, tstruct.summary)
    cap_each = max(m.skill_lines.values(), default=2)
    for s in tstruct.skills:
        n = counts.get(s.category, 0)
        if n > cap_each:
            out.append(Failure(4, "skill_lines", f"skills line '{s.category}' wraps to {n} lines (at most "
                                                 f"{cap_each}); drop a term from it"))
    total = sum(counts.values())
    if total > m.skill_total:
        out.append(Failure(4, "skill_total", f"the skills block is {total} lines; the base fits in {m.skill_total}"))
    if prules.get("skill_rows_fixed"):
        out += _skill_row_problems(tstruct, lines, m.skill_lines, counts, cap_each)
    return out, info


def _skill_row_problems(tstruct: docxio.Structure, lines: list, base_lines: dict, counts: dict, cap: int) -> list:
    """skill_rows_fixed: every row renders in exactly the base's line count (a wrap past it is already reported),
    and a row of 2+ lines never ends with one dangling word."""
    out = []
    texts = skill_row_texts(lines, tstruct)
    for s in tstruct.skills:
        n, want = counts.get(s.category, 0), base_lines.get(s.category)
        if want and n and n < want or (want and n > want and n <= cap):
            out.append(Failure(4, "skill_row_lines", f"skills line '{s.category}' renders in {n} line(s); the "
                                                     f"base's row is exactly {want}: "
                                                     + ("add a term it already allows" if n < want else "drop a term")))
        rows = texts.get(s.category) or []
        last = rows[-1].split() if len(rows) > 1 else []
        if len(last) == 1:
            out.append(Failure(4, "dangling", f"skills line '{s.category}' ends with one dangling word on its last "
                                              f"line ('{last[0]}'): reorder or swap a term so the last line has more"))
    return out


def _numbers(text: str) -> set:
    return {t.lstrip("$").replace(",", "") for t in NUMBER.findall(text)}


_VENDOR = re.compile(r"^(aws|amazon|azure|microsoft|google cloud|gcp|google)\s+", re.I)   # a cloud's own name prefix


def check_honesty(inp: Input, tstruct: docxio.Structure, scores: dict | None) -> tuple:
    out, info = [], {"added_from_jd": [], "unknown_terms": [], "era_violations": [], "bad_numbers": []}
    prules = job_rules(inp)
    found = (scores or {}).get("found") or {}
    categories = (scores or {}).get("categories") or {}
    tailored = set(found.get("tailored") or {})
    allowed = set()
    for key, d in found.items():
        if key not in ("tailored", "jd") and not key.startswith(("era_job_", "skills_row_")):
            allowed |= set(d)
    jd_terms = set((scores or {}).get("jd_found") or {})
    known_names = {squash(k) for k in inp.known_skills}          # known_skills.txt itself (the lexicon scan of
    allowed |= {t for t in tailored if squash(t) in known_names}  # that list misses some names, e.g. 'GCP')
    # the title under the name is picked by the rule from the person's own titles, never a skill claim
    # ("Machine Learning Engineer" from headline_titles on a base that names no "Machine Learning")
    titles = " ".join(prules.get("headline_titles") or ())
    allowed |= {t for t in tailored if titles and re.search(rf"(?i)(?<!\w){re.escape(t)}(?!\w)", titles)}
    unknown = sorted(tailored - allowed)
    if prules["jd_skills_allowed"]:
        info["added_from_jd"] = sorted(t for t in unknown if t in jd_terms)
        unknown = [t for t in unknown if t not in jd_terms]
    notes = [n for n in getattr(inp, "notes", ()) or () if n]
    info["confirmed_by_note"] = sorted(t for t in unknown if any(confirmed.named_in_note(t, n) for n in notes))
    unknown = [t for t in unknown if t not in info["confirmed_by_note"]]
    info["unknown_terms"] = unknown
    info["unknown_categories"] = {t: categories.get(t) for t in unknown}
    if unknown:
        out.append(Failure(5, "unknown_skills", "not in the base resumes or known skills: "
                                                + ", ".join(f"'{t}'" for t in unknown)))
    for j, job in enumerate(tstruct.jobs):
        if not rules.is_era_job(job["company"], prules):
            continue
        era_found = set(found.get(f"era_job_{j}") or {})
        bad = sorted(t for t in era_found if categories.get(t) in rules.GENAI_CATEGORIES or t in rules.GENAI_TERMS)
        if bad:
            info["era_violations"] += bad
            out.append(Failure(5, "era", f"{job['company']} bullets mention GenAI-era tools that did not exist in "
                                         f"that role: {', '.join(bad)}; use classic ML / data engineering wording"))
    if prules.get("jd_gaps"):                       # a JD skill the person lacks is listed, never added
        confirmed_names = {squash(t) for t in info["confirmed_by_note"]}
        info["gaps"] = sorted({t["name"] for t in (scores or {}).get("terms") or []
                               if t["name"] not in allowed and squash(t["name"]) not in known_names
                               and squash(_VENDOR.sub("", t["name"])) not in known_names   # AWS Glue = Glue
                               and squash(t["name"]) not in confirmed_names})
    sources = list(inp.all_base_texts) + [inp.skill_text or ""]
    if prules.get("fact_bank_numbers") and getattr(inp, "fact_bank", ""):   # this base + facts.md above Part 2 + notes
        sources = [inp.base_structure.full_text(), inp.fact_bank, "\n".join(confirmed.read(inp.pid, "facts"))]
    allowed_numbers = set()
    for text in sources + [confirmed.fact_text(n) for n in notes]:
        allowed_numbers |= _numbers(text)
    body = "\n".join([tstruct.summary] + [b for j in tstruct.jobs for b in j["bullets"]])
    bad_numbers = []
    for match in NUMBER.finditer(body):
        core = match.group(0).lstrip("$").replace(",", "")
        if core not in allowed_numbers:
            start = max(0, match.start() - 30)
            bad_numbers.append(f"'{match.group(0)}' in \"…{body[start:match.end() + 20].strip()}…\"")
    if bad_numbers:
        info["bad_numbers"] = bad_numbers
        out.append(Failure(5, "numbers", "numbers that are not in the base resume or fact bank: "
                                         + "; ".join(bad_numbers[:6])))
    return out, info


# ---------- mirrored JD responsibilities (rules.toml mirror_duties) ----------

MIRROR_SHARE = 0.6                    # share of a mirrored bullet's own words (the JD phrase left out) the facts must hold
_STOP = frozenset("a an and the of for to in on with by from at as into its it is are was were that this these those "
                  "or so than then each per via over under across up down both all our their his her them they he "
                  "she we you be been being has have had do did not no only also more most new now".split())


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9+#]{2,}", squash_dashes(text).lower()) if w not in _STOP}


def squash_dashes(text: str) -> str:
    return re.sub(r"[\u2010-\u2015-]", " ", text or "")


def _stem(word: str) -> str:
    return re.sub(r"(ing|ed|es|s)$", "", word) if len(word) > 4 else word


def employer_facts(fact_bank: str, company: str) -> str:
    """Every fact-bank section about one employer ("## Lumen Freight, USA (Mar 2023 to present)" and a later
    "### Lumen Freight" in Part 2), by the company's first word."""
    head = (company or "").split(",")[0].split()[0].lower() if (company or "").strip() else ""
    out, keep = [], False
    for line in (fact_bank or "").splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            keep = line.startswith(("## ", "### ")) and bool(head) and title.startswith(head)
            continue
        if keep:
            out.append(line)
    return "\n".join(out)


def _skill_names(inp: Input) -> list:
    names = list(inp.known_skills)
    for s in inp.base_structure.skills:
        names += [x.strip() for x in re.split(r",(?![^()]*\))", s.items) if x.strip()]
    return [n for n in names if len(n) >= 3 or (len(n) >= 2 and n.isupper())]


def _has(term: str, text: str) -> bool:
    return bool(re.search(rf"(?i)(?<![\w]){re.escape(term)}(?![\w])", text))


# a concept word the facts say another way: an LLM agent is generative AI (an eval run flagged "Generative AI" on a
# bullet about real LLM agent work)
PREFIX_MIN = 4                        # "eval" holds "evaluation"; "ai" / "ml" are too short to hold anything
_SAYS = {"generative": ("llm", "llms", "gpt", "claude", "bedrock", "genai", "gen ai")}


def _held(term: str, facts: str) -> bool:
    """The term is in the facts, word for word or as a concept: every content word of it (stemmed) is there
    ("LLM Fine-tuning" over a LoRA bullet that says "fine-tuned" an "LLM"). A named tool is one word that is not."""
    if _has(term, facts):
        return True
    words = _content_words(term)
    known = {_stem(w) for w in _content_words(facts)}
    return bool(words) and all(_known(_stem(w), known) or any(_has(x, facts) for x in _SAYS.get(w, ())) for w in words)


def _known(stem: str, known: set) -> bool:
    """The word or a short form of it is in the facts: "evaluation" over "eval", "deployment" over "deployed"
    (an eval run flagged "LLM Evaluation" on golden-set work whose facts say "eval")."""
    return stem in known or any(len(k) >= PREFIX_MIN and (stem.startswith(k) or k.startswith(stem)) for k in known
                                if len(stem) >= PREFIX_MIN)


def find_mirrored(tstruct: docxio.Structure, base: docxio.Structure, entry: dict) -> tuple | None:
    """(job index, bullet index) of the tailored bullet carrying entry's phrase in the job its bullet_id names."""
    m = re.match(r"^\s*(\d+)\.(\d+)\s*$", str(entry.get("bullet_id") or ""))
    phrase = squash(squash_dashes(str(entry.get("phrase_used") or ""))).lower()
    if not m or not phrase:
        return None
    j = int(m.group(1)) - 1
    if not (0 <= j < len(tstruct.jobs)) or j >= len(base.jobs):
        return None
    for k, text in enumerate(tstruct.jobs[j]["bullets"]):
        if phrase in squash(squash_dashes(text)).lower():
            return j, k
    return None


def mirror_problem(inp: Input, tstruct: docxio.Structure, entry: dict, at: tuple) -> str | None:
    """Why the bullet carrying a mirrored phrase is not a fact-bank fact (None = it is): its numbers, tools and own
    words must come from the base bullet it rewords plus that employer's fact-bank lines and the confirmed facts;
    a JD domain noun on it must be one the employer's real work touched."""
    prules = job_rules(inp)
    j, k = at
    m = re.match(r"^\s*(\d+)\.(\d+)\s*$", str(entry.get("bullet_id")))
    jb, kb = int(m.group(1)) - 1, int(m.group(2)) - 1
    base_job = inp.base_structure.jobs[jb]
    if not (0 <= kb < len(base_job["bullets"])):
        return f"bullet id {entry.get('bullet_id')} is not a base bullet"
    text, company = tstruct.jobs[j]["bullets"][k], base_job["company"]
    notes = "\n".join(confirmed.read(inp.pid, "facts")) + "\n" + "\n".join(confirmed.fact_text(n) for n in inp.notes or ())
    facts = "\n".join([base_job["bullets"][kb], employer_facts(inp.fact_bank_all or inp.fact_bank, company), notes])
    short = company.split(",")[0]
    bad = sorted(_numbers(text) - _numbers(facts))
    if bad:
        return f"numbers {', '.join(bad)} are not in {short}'s facts"
    tools = [t for t in _skill_names(inp) if _has(t, text) and not _held(t, facts)]
    if tools:
        return f"names {', '.join(sorted(set(tools))[:4])}, which the fact bank does not put at {short}"
    own = _content_words(text) - _content_words(str(entry.get("phrase_used") or ""))
    known = {_stem(w) for w in _content_words(facts)}
    held = [w for w in own if _known(_stem(w), known)]
    if own and len(held) / len(own) < MIRROR_SHARE:
        return (f"only {len(held)} of its {len(own)} own words are in {short}'s facts: "
                + ", ".join(sorted(own - set(held))[:6]))
    allowed = next((v for key, v in (prules.get("employer_domains") or {}).items()
                    if company.lower().startswith(key.lower())), ())
    for noun in inp.mirror.get("jd_domain") or ():
        noun = str(noun).strip()
        if noun and _has(noun, text) and not _held(noun, facts) and not any(
                _has(noun, d) or _has(d, noun) for d in allowed):
            return f"the domain \"{noun}\" is not work {short} touched"
    return None


def check_mirror(inp: Input, tstruct: docxio.Structure) -> tuple:
    """mirror_honesty: a mirrored JD phrase may only sit on a bullet whose core fact is in the fact bank or
    the confirmed facts. A phrase no longer on any bullet (a repair rewrote it) is reported as not covered, never a
    failure. Info: per entry {responsibility, bullet_id, phrase_used, at: [job, bullet] | None, ok}."""
    entries = [e for e in inp.mirror.get("mirrored") or () if isinstance(e, dict)]
    out, info = [], []
    for e in entries:
        at = find_mirrored(tstruct, inp.base_structure, e)
        why = mirror_problem(inp, tstruct, e, at) if at else None
        info.append({"responsibility": str(e.get("responsibility") or ""), "bullet_id": str(e.get("bullet_id") or ""),
                     "phrase_used": str(e.get("phrase_used") or ""), "at": list(at) if at else None,
                     "ok": bool(at) and not why, "why": why or ("" if at else "phrase not on the bullet")})
        if why:
            out.append(Failure(5, "mirror_honesty", f"mirrored \"{e.get('phrase_used')}\" ({e.get('responsibility')}) "
                                                     f"sits on {tstruct.jobs[at[0]]['company'].split(',')[0]} bullet "
                                                     f"{at[1] + 1}, whose core fact is not in the fact bank: {why}. Fix: "
                                                     f"take that phrase and its claim off role {at[0] + 1} bullet "
                                                     f"{at[1] + 1}, say only what the fact bank says about that work, "
                                                     "and count this responsibility as not covered"))
    return out, info


def check_ats(inp: Input, scores: dict | None) -> tuple:
    info = {}
    if not scores:
        return [Failure(6, "ats", "no ATS score available (the keyword model failed; see the log)")], info
    by_id = {s["id"]: s for s in scores.get("scores") or []}
    base, tail = by_id.get(inp.base_id), by_id.get("tailored")
    if base is None or tail is None or tail.get("pct") is None:
        return [Failure(6, "ats", "ATS score missing for the base or the tailored resume")], info
    out = []
    info = {"base_pct": base.get("pct"), "tailored_pct": tail.get("pct"), "vendor": scores.get("vendor_label"),
            "label": scores.get("label"), "matched": tail.get("matched"), "missing": tail.get("missing"),
            "base_missing": base.get("missing")}
    if base.get("pct") is not None and tail["pct"] < base["pct"]:
        out.append(Failure(6, "ats_score", f"ATS score {tail['pct']}% is below the base's {base['pct']}%; cover the "
                                           f"missing JD keywords: {', '.join(tail.get('missing') or [])[:300]}"))
    known = set((scores.get("found") or {}).get("known") or {})
    have = set((scores.get("found") or {}).get("tailored") or {})
    prules = rules.for_profile(inp.pid, inp.profile_rules)
    mirror = prules["jd_skills_allowed"]
    # with JD skills allowed, every REQUIRED JD hard skill must be on the resume (preferred ones are asked
    # for in the prompt and reported); otherwise only the required ones the person already lists
    wanted = [t["name"] for t in scores.get("terms") or [] if t.get("need") and (mirror or t["name"] in known)]
    uncovered = [t for t in wanted if t not in have]
    info["uncovered_known"] = uncovered
    # fixed skill rows keep their line counts and bullets only state fact-bank work, so a known skill MAY be added to
    # a row (the prompt asks for it) but is never forced: listed on the result, not a failure

    if uncovered and not prules.get("skill_rows_fixed"):
        what = "Required JD skills" if mirror else "JD keywords the person knows but"
        out.append(Failure(6, "ats_coverage", f"{what} the resume does not mention: "
                                              + ", ".join(uncovered) + " (add each to Skills and to one bullet)"))
    return out, info


def _words(text: str) -> int:
    return len(text.split())


def _sentences(text: str) -> int:
    return len([s for s in SENTENCE_END.split(text.strip()) if s.strip()])


_SUMMARY_LEAD = re.compile(r"^(?P<lead>[^.]{1,80}?) with \d+\+? years\b")


def summary_title_problem(headline: str, summary: str) -> str | None:
    """the summary opens with the title under the name exactly once ("AI Engineer with 3+ years"), never
    two titles ("AI Engineer Data Scientist with 3+ years") or another title. None when fine."""
    found = _SUMMARY_LEAD.match((summary or "").strip())
    if not found or not headline or found.group("lead").strip() == headline.strip():
        return None
    return (f"the summary opens \"{found.group('lead').strip()} with …\"; it must open with the title under the name, "
            f"once: \"{headline.strip()} with …\"")


def check_writing(inp: Input, tstruct: docxio.Structure) -> list:
    out = []
    prules = job_rules(inp)
    banned = rules.banned_re(prules)
    bodies = [("summary", tstruct.summary)] + [(f"skills '{s.category}'", s.items) for s in tstruct.skills]
    for j, job in enumerate(tstruct.jobs):
        bodies += [(f"bullet {k + 1} of role {j + 1}", b) for k, b in enumerate(job["bullets"])]
    for where, text in bodies:
        hits = sorted({m.group(0).lower() for m in banned.finditer(text)})
        if hits:
            out.append(Failure(7, "banned", f"{where} uses banned wording: {', '.join(hits)}"))
        if text.count(rules.EM_DASH) >= 2:
            out.append(Failure(7, "em_dash_chain", f"{where} chains em dashes; use plain sentences"))
        elif rules.EM_DASH in text and where.startswith("bullet"):
            out.append(Failure(7, "em_dash", f"{where} contains an em dash; bullets never do"))
    lo, hi = (prules.get("bullet_words") or (0, 10 ** 6))[:2]
    for j, job in enumerate(tstruct.jobs):
        for k, b in enumerate(job["bullets"]):
            n = _words(b)
            if n < lo or n > hi:
                out.append(Failure(7, "bullet_words", f"bullet {k + 1} of role {j + 1} ({job['company']}) has {n} "
                                                      f"words; the rule is {lo} to {hi}"))
        verbs = (prules.get("verbs") or {}).get(job["company"].split(",")[0].strip())
        if verbs:
            first = [b.split()[0].rstrip(",.") if b.split() else "" for b in job["bullets"]]
            wrong = [f for f in first if f not in verbs]
            if wrong:
                out.append(Failure(7, "verbs", f"{job['company']} bullets must open with one of {list(verbs)}; "
                                               f"found {wrong}"))
            dupes = sorted({f for f in first if first.count(f) > 1})
            if dupes:
                out.append(Failure(7, "verbs_once", f"{job['company']} uses an opening verb twice: {dupes}"))
    if inp.base_measure.bullets_end_with_period:
        missing = [f"{k + 1} of role {j + 1}" for j, job in enumerate(tstruct.jobs)
                   for k, b in enumerate(job["bullets"]) if not b.rstrip().endswith(".")]
        if missing:
            out.append(Failure(7, "period", "bullets must end with a period like the base: bullet "
                                            + ", ".join(missing)))
    else:
        extra = [f"{k + 1} of role {j + 1}" for j, job in enumerate(tstruct.jobs)
                 for k, b in enumerate(job["bullets"]) if b.rstrip().endswith(".")]
        if extra:
            out.append(Failure(7, "period", "the base's bullets end without a period; bullet " + ", ".join(extra)
                                            + " must not end with one"))
    out += _bullet_shape_problems(tstruct, prules)
    keep = prules.get("summary_keep")
    if keep:
        want, got = _sentence_list(inp.base_structure.summary), _sentence_list(tstruct.summary)
        lo_s, hi_s = keep
        if want[lo_s - 1:hi_s] != got[lo_s - 1:hi_s]:
            out.append(Failure(7, "summary_keep", f"summary sentences {lo_s}-{hi_s} must stay the base's word for "
                                                  f"word: " + " ".join(want[lo_s - 1:hi_s])))
    sw = prules.get("summary_words")
    n = _words(tstruct.summary)
    if sw and not (sw[0] <= n <= sw[1]):
        out.append(Failure(7, "summary_words", f"the summary has {n} words; the rule is {sw[0]} to {sw[1]}"))
    ss = prules.get("summary_sentences")
    got = _sentences(tstruct.summary)
    if ss and not (ss[0] <= got <= ss[1]):
        want = f"exactly {ss[0]}" if ss[0] == ss[1] else f"{ss[0]} to {ss[1]}"
        out.append(Failure(7, "summary_sentences", f"the summary has {got} sentences; the rule is {want}"))
    start = rules.summary_start(prules, inp.base_id)
    if start and not tstruct.summary.startswith(start):
        out.append(Failure(7, "summary_start", f"the summary must open with \"{start}\""))
    stacked = summary_title_problem(tstruct.headline, tstruct.summary)
    if stacked:
        out.append(Failure(7, "summary_title", stacked))
    for must in prules.get("summary_must_contain") or ():
        if must not in tstruct.summary:
            out.append(Failure(7, "summary_contains", f"the summary must contain \"{must}\""))
    loc = prules.get("location")
    if loc and loc not in tstruct.contact:
        out.append(Failure(7, "location", f"the contact line must keep the location {loc}"))
    return out


FROM_TO = re.compile(r"\bfrom\b(?:\s+[^\s.,;:!?]+){1,5}?\s+to\b", re.I)
_PLAIN_BAD = (";", "—", "–", "(", ")")


def _sentence_list(text: str) -> list:
    return [x.strip() for x in SENTENCE_END.split((text or "").strip()) if x.strip()]


def from_to_count(text: str) -> int:
    return len(FROM_TO.findall(text or ""))


def _bullet_shape_problems(tstruct: docxio.Structure, prules: dict) -> list:
    """The bullet-shape rules: opening verbs unique across the resume, varied lengths, plain punctuation, at most N "from

    X to Y" phrasings. Each message names its bullets as "bullet k of role j" so a repair can find them."""
    out = []
    bullets = [(j, k, b) for j, job in enumerate(tstruct.jobs) for k, b in enumerate(job["bullets"])]
    if prules.get("verbs_unique"):
        seen: dict = {}
        for j, k, b in bullets:
            verb = (b.split() or [""])[0].rstrip(",.").lower()
            seen.setdefault(verb, []).append(f"bullet {k + 1} of role {j + 1}")
        for verb, where in seen.items():
            if verb and len(where) > 1:
                out.append(Failure(7, "verbs_unique", f"the opening verb '{verb}' is used {len(where)} times ("
                                                      + ", ".join(where) + "); every bullet opens with its own verb"))
    short = prules.get("short_bullets")
    if short:
        limit, need = short
        have = sum(1 for _, _, b in bullets if _words(b) <= limit)
        if have < need:
            longest = sorted(bullets, key=lambda x: -_words(x[2]))[:need - have]
            out.append(Failure(7, "short_bullets", f"only {have} bullet(s) have {limit} words or fewer; at least "
                                                   f"{need} must (vary the lengths): shorten "
                                                   + ", ".join(f"bullet {k + 1} of role {j + 1}" for j, k, _ in longest)))
    top, cap = (prules.get("bullet_words") or (0, 0))[1], prules.get("max_at_top")
    if cap and top:
        at_top = [(j, k) for j, k, b in bullets if _words(b) == top]
        if len(at_top) > cap:
            out.append(Failure(7, "at_top", f"{len(at_top)} bullets have exactly {top} words (at most {cap}): shorten "
                                            + ", ".join(f"bullet {k + 1} of role {j + 1}" for j, k in at_top[cap:])))
    if prules.get("bullet_plain"):
        for j, k, b in bullets:
            bad = [c for c in _PLAIN_BAD if c in b]
            if bad:
                out.append(Failure(7, "bullet_punct", f"bullet {k + 1} of role {j + 1} contains "
                                                      f"{' '.join(repr(c) for c in bad)}; bullets use no semicolons, "
                                                      f"em/en dashes or parentheses (hyphen ranges like 4-6 are fine)"))
    cap = prules.get("max_from_to")
    if cap is not None:
        hits = [(j, k) for j, k, b in bullets for _ in range(from_to_count(b))]
        if len(hits) > cap:
            out.append(Failure(7, "from_to", f"{len(hits)} \"from X to Y\" phrasings (at most {cap}); say the others "
                                             f"another way: " + ", ".join(f"bullet {k + 1} of role {j + 1}"
                                                                          for j, k in dict.fromkeys(hits))))
    return out


def _overflowing(lines: list, right: float) -> str:
    """The text of the lines that end past `right` (points == px at 72 dpi), for an actionable message."""
    bad = [ln.text for ln in lines if ln.x1 > right + MARGIN_TOL_PX]
    if not bad:
        return ""
    quoted = "; ".join(f'"…{t[-60:]}"' for t in bad[:3])
    hint = (" (Word could not break that line: replace hyphenated compounds such as 'real-time' or 'image-based' "
            "on it with unhyphenated words)" if any("-" in t for t in bad) else "")
    return f" on the line(s) ending {quoted}: reword so the line breaks earlier{hint}"


def protruding_lines(lines: list, column_right: float) -> list:
    """Texts of the lines that end past the base's text column (cosmetic: inside the page, past where every other
    line stops). The mechanical repair tries to break such a line earlier; it is never a failure by itself."""
    return [ln.text for ln in lines if ln.x1 > column_right + MARGIN_TOL_PX]


def overflow_lines(inp: Input, pdf: Path, info: pdfx.Info, ink: tuple | None) -> int:
    """How many rendered lines the resume is too long by: the lines on page 2+, or the ink below the page box
    divided by the line pitch (at least 1 when anything is below it)."""
    if info.pages > 1:
        extra = sum(len(pdfx.flat_lines(pdfx.lines(pdf, page))) for page in range(2, info.pages + 1))
        return max(1, extra)
    if ink is None:
        return 0
    over = ink[3] - inp.base_measure.margins[3]
    if over <= MARGIN_TOL_PX:
        return 0
    return max(1, int(-(-over // max(6.0, inp.base_measure.line_height))))


def check_visual(inp: Input, pdf: Path, lines: list | None = None) -> tuple:
    """Check 8 against the REAL page box (the base .docx's pgSz/pgMar) with a 4 px tolerance; the base
    PDF's own ink box is reported for reference only. Lines past the base's text column are listed as
    'protruding' (info) so the mechanical repair can try to break them earlier."""
    out = []
    w, h, rows = pdfx.raster(pdf, 72)
    ink = pdfx.ink_bbox(w, h, rows)
    ml, mt, mr, mb = inp.base_measure.margins
    info = {"ink": ink, "base_ink": inp.base_measure.ink, "page_box": [ml, mt, mr, mb],
            "protruding": protruding_lines(lines or [], inp.base_measure.column_right)}
    if ink is None:
        return [Failure(8, "blank", "page 1 is blank")], info
    l, t, r, b = ink
    if l < ml - MARGIN_TOL_PX:
        out.append(Failure(8, "left_margin", f"ink starts {ml - l:.0f} px left of the page's left margin"))
    if r > mr + MARGIN_TOL_PX:
        out.append(Failure(8, "right_margin", f"text runs {r - mr:.0f} px past the page's right margin"
                                              + _overflowing(lines or [], mr)))
    if t < mt - MARGIN_TOL_PX:
        out.append(Failure(8, "top_margin", f"ink starts {mt - t:.0f} px above the page's top margin"))
    if b > mb + MARGIN_TOL_PX:
        out.append(Failure(8, "bottom_margin", f"text runs {b - mb:.0f} px below the page's bottom margin"))
    bands = pdfx.bands(w, h, rows)
    tallest = max((x.height for x in bands), default=0)
    info["band_max"] = tallest
    if tallest > inp.base_measure.band_max + BAND_TOL_PX:
        out.append(Failure(8, "overlap", f"a text band is {tallest} px tall (base max {inp.base_measure.band_max}): "
                                         f"lines overlap or a line is taller than the base allows"))
    return out, info


def run_all(inp: Input) -> Report:
    report = Report()
    tdoc = docxio.read(inp.docx_path)
    tstruct = docxio.structure(tdoc)
    info = pdfx.info(inp.pdf_path)
    report.failures += check_page(inp, info, tdoc)
    lines = pdfx.flat_lines(pdfx.lines(inp.pdf_path)) if info.pages >= 1 else []
    report.failures += check_layout(inp, tstruct, lines)
    report.failures += check_fonts(inp, tdoc, inp.pdf_path)
    fit_failures, fit_info = check_fit(inp, tstruct, lines)
    report.failures += fit_failures
    honesty_failures, honesty_info = check_honesty(inp, tstruct, inp.scores)
    report.failures += honesty_failures
    ats_failures, ats_info = check_ats(inp, inp.scores)
    report.failures += ats_failures
    report.failures += check_writing(inp, tstruct)
    mirror_failures, mirror_info = check_mirror(inp, tstruct) if getattr(inp, "mirror", None) else ([], [])
    report.failures += mirror_failures
    visual_failures, visual_info = check_visual(inp, inp.pdf_path, lines)
    report.failures += visual_failures
    from . import tailorchecks                                # (it imports this module)
    tailor_failures, tailor_info = tailorchecks.run(inp, tstruct)
    report.failures += tailor_failures
    report.info = {"fit": fit_info, "honesty": honesty_info, "ats": ats_info, "visual": visual_info,
                   **({"mirror": mirror_info} if mirror_info else {}),
                   **({"tailor": tailor_info} if tailor_info else {}),
                   "pages": info.pages, "producer": info.producer,
                   "overflow_lines": overflow_lines(inp, inp.pdf_path, info, visual_info.get("ink"))}
    report.failures.sort(key=lambda f: f.check)
    return report


def era_texts(structure: docxio.Structure, pid: str, profile_rules: dict | None = None) -> dict:
    """{'era_job_<j>': bullets text} for the jobs the era rule covers (scored by ats.score as extra texts), plus
    {'skills_row_<i>': row items} when the person's rules add JD skills by row (skills_add_first: a missing JD skill's
    row is the one whose skills share its lexicon category)."""
    prules = rules.for_profile(pid, profile_rules)
    out = {f"era_job_{j}": "\n".join(job["bullets"]) for j, job in enumerate(structure.jobs)
           if rules.is_era_job(job["company"], prules)}
    if prules.get("skills_add_first"):
        out.update({f"skills_row_{i}": s.items for i, s in enumerate(structure.skills)})
    return out
