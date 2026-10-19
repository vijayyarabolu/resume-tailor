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


def check_layout(inp: Input, tstruct: docxio.Structure, lines: list | None = None) -> list:
    prules = job_rules(inp)
    want = prules.get("headline")
    may = prules["headline_may_change"] or bool(want)
    out = [Failure(2, "layout", m) for m in docxio.layout_diffs(inp.base_structure, tstruct, may,
                                                                 rules.contact_location(prules))]
    if want and tstruct.headline != want:                     # the job family's title, set by Python
        out.append(Failure(2, "layout", f"headline is {tstruct.headline!r}; the title under the name must be "
                                        f"{want!r} (the plain title for this posting's family)"))
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
    return out, info


def _numbers(text: str) -> set:
    return {t.lstrip("$").replace(",", "") for t in NUMBER.findall(text)}


def check_honesty(inp: Input, tstruct: docxio.Structure, scores: dict | None) -> tuple:
    out, info = [], {"added_from_jd": [], "unknown_terms": [], "era_violations": [], "bad_numbers": []}
    prules = job_rules(inp)
    found = (scores or {}).get("found") or {}
    categories = (scores or {}).get("categories") or {}
    tailored = set(found.get("tailored") or {})
    allowed = set()
    for key, d in found.items():
        if key not in ("tailored", "jd") and not key.startswith("era_job_"):
            allowed |= set(d)
    jd_terms = set((scores or {}).get("jd_found") or {})
    known_names = {squash(k) for k in inp.known_skills}          # known_skills.txt itself (the lexicon scan of
    allowed |= {t for t in tailored if squash(t) in known_names}  # that list misses some names, e.g. 'GCP')
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
    allowed_numbers = set()
    for text in list(inp.all_base_texts) + [inp.skill_text or ""] + [confirmed.fact_text(n) for n in notes]:
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
    mirror = rules.for_profile(inp.pid, inp.profile_rules)["jd_skills_allowed"]
    # with JD skills allowed, every REQUIRED JD hard skill must be on the resume (preferred ones are asked
    # for in the prompt and reported); otherwise only the required ones the person already lists
    wanted = [t["name"] for t in scores.get("terms") or [] if t.get("need") and (mirror or t["name"] in known)]
    uncovered = [t for t in wanted if t not in have]
    info["uncovered_known"] = uncovered
    if uncovered:
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
    visual_failures, visual_info = check_visual(inp, inp.pdf_path, lines)
    report.failures += visual_failures
    report.info = {"fit": fit_info, "honesty": honesty_info, "ats": ats_info, "visual": visual_info,
                   "pages": info.pages, "producer": info.producer,
                   "overflow_lines": overflow_lines(inp, inp.pdf_path, info, visual_info.get("ink"))}
    report.failures.sort(key=lambda f: f.check)
    return report


def era_texts(structure: docxio.Structure, pid: str, profile_rules: dict | None = None) -> dict:
    """{'era_job_<j>': bullets text} for the jobs the era rule covers (scored by the bridge as extra texts)."""
    prules = rules.for_profile(pid, profile_rules)
    return {f"era_job_{j}": "\n".join(job["bullets"]) for j, job in enumerate(structure.jobs)
            if rules.is_era_job(job["company"], prules)}
