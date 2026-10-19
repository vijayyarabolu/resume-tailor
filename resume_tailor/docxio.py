"""Stdlib .docx reader/filler (zipfile + ElementTree). No python-docx.

read(path)      -> Doc: the paragraphs of word/document.xml classified by position and heading (name, headline,
                   contact, heading, summary, skill, edu, job_header, job_title, bullet, blank, other).
structure(doc)  -> Structure: headline, summary, skill lines [{category, items}], jobs [{company, dates, title,
                   bullets}], education, section order. Skill lines may share one paragraph (Google Docs exports
                   put several "Category: items" lines in one paragraph separated by <w:br/>).
fill(doc, content) -> bytes of a new .docx: text replaced INSIDE the existing runs, so every paragraph property,
                   run property, tab stop, numbering and section setting stays byte-for-byte what the base had.
                   Only word/document.xml changes; every other zip member is copied as is.
"""
import copy
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SECTION_NAMES = {"summary": "summary", "professional summary": "summary", "skills": "skills",
                 "technical skills": "skills", "education": "education", "work experience": "experience",
                 "experience": "experience", "professional experience": "experience", "projects": "projects"}
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH}\s+\d{{4}}|\d{{4}})"
DATES = re.compile(rf"^\s*(?P<dates>{_DATE}\s*[–—-]\s*(?:Present|Current|Now|{_DATE}))\s*$", re.I)
ONE_DATE = re.compile(rf"^\s*(?P<dates>{_MONTH}\s+\d{{4}}|(?:Expected|Graduated)\s+{_MONTH}\s+\d{{4}})\s*$", re.I)
PADDING = re.compile(r"\t[ \t ]*|[  ]{3,}")
_BULLET_GLYPH = re.compile(r"^\s*[•●▪◦\-–*]\s+")
_RSID = re.compile(r'\s+w:rsid\w*="[^"]*"')
_ROOT_TAG = re.compile(r"<w:document\b[^>]*>")
_XMLNS = re.compile(r'xmlns:([A-Za-z0-9_]+)="([^"]+)"')


class DocxError(ValueError):
    pass


@dataclass
class Para:
    index: int
    el: ET.Element
    kind: str
    text: str


@dataclass
class Doc:
    members: dict                     # zip name -> bytes, original order
    order: list
    root: ET.Element
    root_tag: str                     # the original <w:document ...> start tag (namespace declarations)
    paras: list = field(default_factory=list)


@dataclass
class SkillLine:
    category: str
    items: str
    para: int                         # index into doc.paras
    group: int                        # group inside that paragraph (br-separated)


@dataclass
class Structure:
    name: str
    headline: str
    contact: str
    summary: str
    summary_paras: list
    skills: list                      # [SkillLine]
    jobs: list                        # [{company, dates, title, bullets:[str], bullet_paras:[int], header_para}]
    education: list                   # [str]
    sections: list                    # heading texts in order
    headline_para: int
    contact_para: int = -1            # the contact line's paragraph (its location follows the job)

    def as_content(self) -> dict:
        return {"headline": self.headline, "summary": self.summary,
                "skills": [{"category": s.category, "items": split_items(s.items)} for s in self.skills],
                "jobs": [{"company": j["company"], "dates": j["dates"], "title": j["title"],
                          "bullets": list(j["bullets"])} for j in self.jobs],
                "education": list(self.education), "sections": list(self.sections)}

    def body_text(self) -> str:
        """Headline + summary + skills + bullets: the text a tailoring may change (honesty and ATS checks)."""
        parts = [self.headline, self.summary] + [f"{s.category}: {s.items}" for s in self.skills]
        for j in self.jobs:
            parts += j["bullets"]
        return "\n".join(parts)

    def full_text(self) -> str:
        parts = [self.name, self.headline, self.contact, self.summary]
        parts += [f"{s.category}: {s.items}" for s in self.skills]
        for j in self.jobs:
            parts += [f"{j['company']} {j['dates']}", j["title"], *j["bullets"]]
        parts += self.education
        return "\n".join(parts)


# ---------- reading ----------

_ODD_SPACES = re.compile(r"[ \t\u00a0\u2007\u202f]+")
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def _clean(text: str) -> str:
    """Plain spaces only: non-breaking spaces/hyphens and zero-width characters would stop Word from wrapping."""
    text = _INVISIBLE.sub("", text).replace("\u2011", "-")
    return _ODD_SPACES.sub(" ", text).strip()


def split_items(items: str) -> list[str]:
    """'AWS (S3, Glue), GCP' -> ['AWS (S3, Glue)', 'GCP'] (commas inside parentheses do not split)."""
    parts, depth, current = [], 0, []
    for ch in items:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [_clean(p) for p in parts if _clean(p)]


def runs_of(p: ET.Element) -> list[ET.Element]:
    """Text-bearing runs in order, including those inside hyperlinks and smart tags."""
    out = []
    for child in p:
        if child.tag == W + "r":
            out.append(child)
        elif child.tag in (W + "hyperlink", W + "smartTag", W + "ins"):
            out.extend(r for r in child.iter(W + "r"))
    return out


def run_text(r: ET.Element) -> str:
    bits = []
    for child in r:
        if child.tag == W + "t":
            bits.append(child.text or "")
        elif child.tag == W + "tab":
            bits.append("\t")
        elif child.tag == W + "br":
            bits.append("\n")
    return "".join(bits)


def para_text(p: ET.Element) -> str:
    return "".join(run_text(r) for r in runs_of(p))


def _is_list_item(p: ET.Element, styles_numbered: set) -> bool:
    ppr = p.find(W + "pPr")
    if ppr is not None and ppr.find(W + "numPr") is not None:
        return True
    style = ppr.find(W + "pStyle") if ppr is not None else None
    if style is not None and style.get(W + "val") in styles_numbered:
        return True
    return bool(_BULLET_GLYPH.match(para_text(p)))


def _numbered_styles(members: dict) -> set:
    raw = members.get("word/styles.xml")
    if not raw:
        return set()
    out = set()
    for style in ET.fromstring(raw).iter(W + "style"):
        sid = style.get(W + "styleId") or ""
        name = style.find(W + "name")
        label = (name.get(W + "val") if name is not None else "") or ""
        if style.find(f"{W}pPr/{W}numPr") is not None or "list" in label.lower():
            out.add(sid)
    return out


def padded_span(text: str, single: bool = False):
    for m in PADDING.finditer(text):
        rest = text[m.end():]
        if text[:m.start()].strip() and (DATES.match(rest) or (single and ONE_DATE.match(rest))):
            return m.span()
    return None


def split_dated(text: str, single: bool = False):
    span = padded_span(text, single)
    return (_clean(text[:span[0]]), _clean(text[span[1]:])) if span else None


def _register_namespaces(raw: str) -> str:
    match = _ROOT_TAG.search(raw)
    root_tag = match.group(0) if match else ""
    for prefix, uri in _XMLNS.findall(root_tag):
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            pass
    return root_tag


def read(path_or_bytes) -> Doc:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        with open(path_or_bytes, "rb") as fh:
            data = fh.read()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise DocxError(f"not a .docx: {err}") from err
    order = archive.namelist()
    members = {name: archive.read(name) for name in order}
    if "word/document.xml" not in members:
        raise DocxError("not a .docx: word/document.xml missing")
    raw = members["word/document.xml"].decode("utf-8")
    root_tag = _register_namespaces(raw)
    root = ET.fromstring(raw)
    doc = Doc(members, order, root, root_tag)
    doc.paras = _walk(doc)
    return doc


def _walk(doc: Doc) -> list[Para]:
    body = doc.root.find(W + "body")
    if body is None:
        raise DocxError("document.xml has no body")
    numbered = _numbered_styles(doc.members)
    out, section, preamble, prev = [], None, 0, None
    for i, p in enumerate(body.findall(W + "p")):
        text = _clean(para_text(p))
        heading = SECTION_NAMES.get(text.rstrip(":").lower())
        if not text:
            kind = "blank"
        elif heading:
            kind, section = "heading", heading
        elif section is None:
            kind = ("name", "headline", "contact")[preamble] if preamble < 3 else "preamble"
            preamble += 1
        elif section == "summary":
            kind = "summary"
        elif section == "skills":
            kind = "skill" if ":" in text else "other"
        elif section == "education":
            kind = "edu"
        elif section == "experience":
            kind = ("job_header" if split_dated(para_text(p)) else "bullet" if _is_list_item(p, numbered)
                    else "job_title" if prev == "job_header" else "other")
        else:
            kind = "other"
        out.append(Para(i, p, kind, text))
        prev = kind if kind != "blank" else prev
    return out


# ---------- skill groups ----------

def _is_bold(r: ET.Element) -> bool:
    rpr = r.find(W + "rPr")
    b = rpr.find(W + "b") if rpr is not None else None
    return b is not None and b.get(W + "val") not in ("0", "false")


def skill_groups(p: ET.Element) -> list[list[ET.Element]]:
    """Runs of a skills paragraph split into 'Category: items' groups at <w:br/> boundaries. A run whose <w:br/>
    comes before its text (Word: `<w:br/><w:t>LLMs: </w:t>`) starts the next group; a run whose <w:br/> comes after
    its text (Google's export: `<w:t>…Neo4j</w:t><w:br/>`) ends the current one."""
    groups, current = [], []
    for r in runs_of(p):
        trailing = _br_after_text(r)
        if r.find(W + "br") is not None and current and not trailing:
            groups.append(current)
            current = []
        current.append(r)
        if trailing:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [g for g in groups if "".join(run_text(r) for r in g).strip()]


def _br_after_text(r: ET.Element) -> bool:
    """True when a <w:br/> follows text in the same run (the break closes that text's line)."""
    seen_text = False
    for child in r:
        if child.tag == W + "t" and (child.text or "").strip():
            seen_text = True
        elif child.tag == W + "br" and seen_text:
            return True
    return False


def _group_parts(group: list[ET.Element]) -> tuple[str, str]:
    label = "".join(run_text(r) for r in group if _is_bold(r)).replace("\n", "")
    items = "".join(run_text(r) for r in group if not _is_bold(r)).replace("\n", "")
    if ":" not in label and ":" in items:                 # label not bold in this template: split on the colon
        joined = label + items
        label, _, items = joined.partition(":")
        label += ":"
    return _clean(label).rstrip(":").rstrip(), _clean(items)


def structure(doc: Doc) -> Structure:
    s = Structure("", "", "", "", [], [], [], [], [], -1)
    summary = []
    for para in doc.paras:
        p, kind = para.el, para.kind
        if kind == "name":
            s.name = para.text
        elif kind == "headline":
            s.headline, s.headline_para = para.text, para.index
        elif kind == "contact":
            s.contact, s.contact_para = para.text, para.index
        elif kind == "heading":
            s.sections.append(para.text)
        elif kind == "summary":
            summary.append(para.text)
            s.summary_paras.append(para.index)
        elif kind == "skill":
            for g, group in enumerate(skill_groups(p)):
                category, items = _group_parts(group)
                s.skills.append(SkillLine(category, items, para.index, g))
        elif kind == "edu":
            s.education.append(para.text)
        elif kind == "job_header":
            company, dates = split_dated(para_text(p))
            s.jobs.append({"company": company, "dates": dates, "title": "", "bullets": [], "bullet_paras": [],
                           "header_para": para.index, "title_para": None})
        elif kind == "job_title" and s.jobs:
            s.jobs[-1]["title"], s.jobs[-1]["title_para"] = para.text, para.index
        elif kind == "bullet" and s.jobs:
            s.jobs[-1]["bullets"].append(_clean(_BULLET_GLYPH.sub("", para_text(p))))
            s.jobs[-1]["bullet_paras"].append(para.index)
    s.summary = " ".join(summary)
    return s


def validate_structure(s: Structure) -> list[str]:
    problems = [f"missing {k}" for k in ("name", "headline", "summary") if not getattr(s, k)]
    problems += [f"no {k}" for k in ("skills", "jobs", "education") if not getattr(s, k)]
    problems += [f"{j['company']}: no title or bullets" for j in s.jobs if not j["title"] or not j["bullets"]]
    return problems


# ---------- filling ----------

def _set_run_text(r: ET.Element, text: str) -> None:
    """Replace the run's text with `text`, keeping its rPr and any <w:br/>/<w:tab/> that come before the text."""
    for t in r.findall(W + "t"):
        r.remove(t)
    if text:
        t = ET.SubElement(r, W + "t")
        t.text = text
        if text != text.strip() or "  " in text:
            t.set(XML_SPACE, "preserve")


def _text_runs(runs: list[ET.Element]) -> list[ET.Element]:
    return [r for r in runs if r.find(W + "t") is not None]


def _clear_runs(p: ET.Element, runs: list[ET.Element]) -> None:
    """Drop text from runs; a run left with nothing but rPr is removed from the paragraph (or its hyperlink)."""
    parents = {}
    for holder in p.iter():
        for child in holder:
            parents[child] = holder
    for r in runs:
        for t in r.findall(W + "t"):
            r.remove(t)
        if all(child.tag == W + "rPr" for child in r):
            parent = parents.get(r)
            if parent is not None:
                parent.remove(r)


def set_para_text(p: ET.Element, text: str) -> None:
    """Whole paragraph -> text, in the first text run (its rPr kept); the other text runs lose their text."""
    runs = _text_runs(runs_of(p))
    if not runs:
        raise DocxError("paragraph has no text run to fill")
    if _clean(para_text(p)) == _clean(text):
        return
    _set_run_text(runs[0], text)
    _clear_runs(p, runs[1:])


def replace_in_para(p: ET.Element, old: str, new: str) -> None:
    """`old` -> `new` in the paragraph's text, where `old` may span adjacent runs ("Portland", ", ",
    "KS"): the first run it touches takes the new text (with that run's own text before it), the others lose only
    the part `old` covered. Runs outside the span (the contact line's LinkedIn / GitHub links) stay as they are."""
    runs = _text_runs(runs_of(p))
    texts = [run_text(r) for r in runs]
    joined = "".join(texts)
    at = joined.find(old)
    if at < 0:
        raise DocxError(f"{old!r} is not on the contact line")
    end, pos = at + len(old), 0
    for run, text in zip(runs, texts):
        start, stop = pos, pos + len(text)
        pos = stop
        if stop <= at or start >= end:
            continue
        keep_head = text[:max(0, at - start)]
        keep_tail = text[max(0, end - start):] if stop > end else ""
        first = start <= at < stop
        _set_run_text(run, keep_head + (new if first else "") + keep_tail)


def set_skill_group(p: ET.Element, group: list[ET.Element], category: str, items: str) -> None:
    bold = [r for r in group if _is_bold(r) and run_text(r).strip()]
    plain = [r for r in group if not _is_bold(r) and run_text(r).strip()]
    if not bold or not plain:
        raise DocxError("skills line needs a bold category run and a normal items run")
    category = _clean(category).rstrip(":").rstrip()
    current_label, current_items = _group_parts(group)
    if current_label == category and current_items == _clean(items):
        return
    _set_run_text(bold[0], f"{category}: ")
    _set_run_text(plain[0], items)
    _clear_runs(p, [r for r in group if r is not bold[0] and r is not plain[0] and run_text(r).strip()])


MIN_PADDING = 3                       # a padding run never shrinks below this (PADDING still recognises it)
TWIPS_PER_PT = 20.0


def padding_runs(p: ET.Element) -> list[ET.Element]:
    """The whitespace-only runs (3+ spaces) that push a header's dates to the right edge."""
    return [r for r in runs_of(p) if r.find(W + "tab") is None and r.find(W + "br") is None
            and run_text(r).strip() == "" and len(run_text(r)) >= MIN_PADDING]


def tab_padding_runs(p: ET.Element) -> list[ET.Element]:
    """Whitespace-only runs right after a tab ('Acme, India<tab>"  "Mar 2021 - Jul 2023'): the tab already
    places the date, so these spaces are padding too and may go entirely."""
    out, after_tab = [], False
    for r in runs_of(p):
        text = run_text(r)
        is_space = r.find(W + "br") is None and text != "" and text.strip() == ""
        if after_tab and is_space and r.find(W + "tab") is None:
            out.append(r)
            continue
        after_tab = r.find(W + "tab") is not None and text.replace("\t", "").strip() == ""
    return out


def trimmable(p: ET.Element) -> int:
    """Padding spaces this paragraph can still lose (typed padding keeps MIN_PADDING, tab padding can go)."""
    return sum(len(run_text(r)) - MIN_PADDING for r in padding_runs(p)) + sum(len(run_text(r))
                                                                             for r in tab_padding_runs(p))


def trim_padding(p: ET.Element, spaces: int) -> int:
    """Shorten the paragraph's typed-space padding by `spaces` (never below MIN_PADDING), then the spaces after a
    tab. Returns the number actually removed. Google Docs ignores the base's negative character spacing on these
    runs, so its line grows and the dates wrap; fewer spaces keep them on one line (the text, cleaned, is
    unchanged)."""
    removed = 0
    plan = [(r, MIN_PADDING) for r in padding_runs(p)] + [(r, 0) for r in tab_padding_runs(p)]
    for r, keep in plan:
        if removed >= spaces:
            break
        text = run_text(r)
        take = min(spaces - removed, len(text) - keep)
        if take <= 0:
            continue
        _set_run_text(r, text[:len(text) - take])
        removed += take
    return removed


def _ind_right_twips(doc: Doc, p: ET.Element) -> int:
    ppr = p.find(W + "pPr")
    ind = ppr.find(W + "ind") if ppr is not None else None
    if ind is not None and ind.get(W + "right") is not None:
        return int(ind.get(W + "right"))
    style = ppr.find(W + "pStyle") if ppr is not None else None
    sid = style.get(W + "val") if style is not None else None
    raw = doc.members.get("word/styles.xml")
    if sid and raw:
        for st in ET.fromstring(raw).iter(W + "style"):
            if st.get(W + "styleId") == sid:
                sind = st.find(f"{W}pPr/{W}ind")
                if sind is not None and sind.get(W + "right") is not None:
                    return int(sind.get(W + "right"))
    return 0


def page_box(doc: Doc) -> tuple:
    """(left, top, right, bottom) edges of the printable area in points from the section's pgSz/pgMar: the real
    page margins, not where the base's widest line happens to end."""
    sect = doc.root.find(f"{W}body/{W}sectPr")
    size = sect.find(W + "pgSz") if sect is not None else None
    mar = sect.find(W + "pgMar") if sect is not None else None
    width = int(size.get(W + "w")) / TWIPS_PER_PT if size is not None and size.get(W + "w") else 612.0
    height = int(size.get(W + "h")) / TWIPS_PER_PT if size is not None and size.get(W + "h") else 792.0

    def m(key: str) -> float:
        return int(mar.get(W + key) or 0) / TWIPS_PER_PT if mar is not None else 0.0
    return m("left"), m("top"), width - m("right"), height - m("bottom")


def para_right_edge(doc: Doc, p: ET.Element) -> float:
    """Where this paragraph's text column ends (points): page right edge minus its right indent."""
    return page_box(doc)[2] - _ind_right_twips(doc, p) / TWIPS_PER_PT


def fill(doc_or_path, content: dict, pad_trim: dict | None = None) -> bytes:
    """content: {headline?, summary, skills: [{category, items: [str] | str}], jobs: [{bullets: [str]}]}.
    Counts must match the base exactly (that is the layout contract); otherwise DocxError.
    pad_trim: {paragraph index: spaces to remove from that header's date padding} (see trim_padding)."""
    doc = doc_or_path if isinstance(doc_or_path, Doc) else read(doc_or_path)
    doc = copy.deepcopy(doc)
    base = structure(doc)
    skills = content.get("skills") or []
    jobs = content.get("jobs") or []
    if len(skills) != len(base.skills):
        raise DocxError(f"skills: {len(skills)} lines given, the base has {len(base.skills)}")
    if len(jobs) != len(base.jobs):
        raise DocxError(f"jobs: {len(jobs)} given, the base has {len(base.jobs)}")
    for job, want in zip(base.jobs, jobs):
        if len(want.get("bullets") or []) != len(job["bullets"]):
            raise DocxError(f"{job['company']}: {len(want.get('bullets') or [])} bullets given, the base has "
                            f"{len(job['bullets'])}")
    paras = {p.index: p.el for p in doc.paras}
    for old, new in content.get("contact_edits") or []:     # the person's own edit of the contact line
        if old and old != new and base.contact_para >= 0:
            replace_in_para(paras[base.contact_para], old, new)
    place = content.get("contact_location")                  # (the base's location, the job's)
    if place and place[0] != place[1] and base.contact_para >= 0:
        replace_in_para(paras[base.contact_para], place[0], place[1])
    headline = content.get("headline")
    if headline and base.headline_para >= 0:
        set_para_text(paras[base.headline_para], _clean(headline))
    summary = _clean(content.get("summary") or "")
    if not summary:
        raise DocxError("summary is empty")
    set_para_text(paras[base.summary_paras[0]], summary)
    for extra in base.summary_paras[1:]:
        set_para_text(paras[extra], "")
    groups_cache: dict[int, list] = {}
    for line, want in zip(base.skills, skills):
        items = want["items"]
        items = ", ".join(_clean(i) for i in items) if isinstance(items, list) else _clean(str(items))
        p = paras[line.para]
        groups = groups_cache.setdefault(line.para, skill_groups(p))
        set_skill_group(p, groups[line.group], _clean(want["category"]), items)
    for job, want in zip(base.jobs, jobs):
        for idx, text in zip(job["bullet_paras"], want["bullets"]):
            set_para_text(paras[idx], _clean(text))
    for idx, spaces in (pad_trim or {}).items():
        if int(idx) in paras and spaces > 0:
            trim_padding(paras[int(idx)], int(spaces))
    return serialize(doc)


def serialize(doc: Doc) -> bytes:
    xml = ET.tostring(doc.root, encoding="unicode")
    if doc.root_tag:
        xml = _ROOT_TAG.sub(lambda _: doc.root_tag, xml, count=1)
    body = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + xml
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in doc.order:
            archive.writestr(name, body.encode("utf-8") if name == "word/document.xml" else doc.members[name])
    return out.getvalue()


# ---------- comparing (checks 2 and 3) ----------

def _canon(el) -> str:
    if el is None:
        return ""
    return _RSID.sub("", ET.tostring(el, encoding="unicode"))


def para_props(p: ET.Element) -> str:
    return _canon(p.find(W + "pPr"))


def run_props(p: ET.Element) -> list[str]:
    return [_canon(r.find(W + "rPr")) for r in runs_of(p) if r.find(W + "t") is not None]


def format_diffs(base: Doc, other: Doc) -> list[str]:
    """Where `other` formats differently from `base`: paragraph count, paragraph properties, run properties of
    text runs (each text run in `other` must carry a run style the same base paragraph already had, and the first
    text run must match the base's first), and the style/numbering/settings/font zip members byte for byte."""
    problems = []
    for name in ("word/styles.xml", "word/numbering.xml", "word/settings.xml", "word/fontTable.xml",
                 "word/theme/theme1.xml"):
        if base.members.get(name) != other.members.get(name):
            problems.append(f"{name} differs from the base")
    if len(base.paras) != len(other.paras):
        problems.append(f"paragraph count {len(other.paras)} (base {len(base.paras)})")
        return problems
    for a, b in zip(base.paras, other.paras):
        if para_props(a.el) != para_props(b.el):
            problems.append(f"paragraph {a.index} ({a.kind}): paragraph properties changed")
        ra, rb = run_props(a.el), run_props(b.el)
        if ra and rb and ra[0] != rb[0]:
            problems.append(f"paragraph {a.index} ({a.kind}): first run font/size changed")
        extra = [x for x in rb if x not in ra]
        if extra:
            problems.append(f"paragraph {a.index} ({a.kind}): a run has a font/size the base paragraph never had")
    return problems


def layout_diffs(base: Structure, other: Structure, headline_may_change: bool = True,
                 contact_location: tuple | None = None) -> list[str]:
    """Check 2: same sections in order, same jobs (company, dates, title char for char), same bullet counts, same
    education and contact line (with only its location swapped for the job's), same number of skill
    lines."""
    problems = []
    if base.sections != other.sections:
        problems.append(f"sections differ: {other.sections} (base {base.sections})")
    if base.name != other.name:
        problems.append(f"name changed: {other.name!r}")
    contact = base.contact.replace(*contact_location, 1) if contact_location else base.contact
    if contact != other.contact:
        problems.append("contact line changed" + (f" (it must read {contact!r})" if contact != base.contact else ""))
    if not headline_may_change and base.headline != other.headline:
        problems.append(f"headline changed: {other.headline!r}; the title under the name must stay the base's own "
                        f"title {base.headline!r}")
    if base.education != other.education:
        problems.append(f"education changed: {other.education}")
    if len(base.skills) != len(other.skills):
        problems.append(f"{len(other.skills)} skill lines (base {len(base.skills)})")
    if len(base.jobs) != len(other.jobs):
        problems.append(f"{len(other.jobs)} roles (base {len(base.jobs)})")
    for n, (a, b) in enumerate(zip(base.jobs, other.jobs), 1):
        for key in ("company", "dates", "title"):
            if a[key] != b[key]:
                problems.append(f"role {n} {key}: {b[key]!r} (base {a[key]!r})")
        if len(a["bullets"]) != len(b["bullets"]):
            problems.append(f"role {n} ({a['company']}): {len(b['bullets'])} bullets (base {len(a['bullets'])})")
    return problems
