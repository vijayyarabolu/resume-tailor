"""A transparent ATS keyword model. Real applicant tracking systems differ by vendor and none publish their
scoring, so this does the part they share: find the hard skills a job description names, decide which are
required and which are nice to have, and measure how many of them each resume covers.

score(jd_text, resumes, ...) returns the shape the pipeline and the checks read:
    {"vendor", "vendor_label", "modeled", "label",
     "terms": [{name, weight, need, count, forms, importance}],
     "scores": [{id, label, pct, matched, missing}], "best_id",
     "found": {resume id or extra key: {canonical: count}}, "categories": {canonical: category}, "jd_found"}

Skills come from lexicon.json (plus <home>/lexicon.json for your own terms). Matching is whole-word and case
insensitive, except very short names (R, Go, C#) which must appear as written.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from . import config, headline

LEXICON = Path(__file__).resolve().parent / "lexicon.json"
SHORT = 2                                   # names this short match case-sensitively
PREFERRED = re.compile(r"\b(prefer\w*|nice[- ]to[- ]have|bonus|plus|desired|good[- ]to[- ]have|nice[- ]to)\b", re.I)
REQUIRED = re.compile(r"\b(requir\w*|qualifications?|must|need|looking for|you have|you bring|skills|experience|"
                      r"responsibilit\w*|what you.ll do|the role|about you)\b", re.I)
HEADING_MAX = 70
BULLET = re.compile(r"^\s*(?:[-*•●▪◦]|\d+[.)])\s+")


def _extra_path() -> Path:
    return config.DATA / "lexicon.json"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def lexicon() -> dict:
    """{canonical: (category, (forms...))} from the bundled file and the user's own."""
    return _lexicon(_mtime(_extra_path()))


@lru_cache(maxsize=4)
def _lexicon(extra_mtime: float) -> dict:
    out = {}
    for path in (LEXICON, _extra_path()):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for category, terms in data.items():
            if category.startswith("_") or not isinstance(terms, dict):
                continue
            for name, forms in terms.items():
                forms = [f for f in (forms or []) if isinstance(f, str)]
                out[name] = (category, tuple(dict.fromkeys([name, *forms])))
    return out


def _pattern(form: str) -> re.Pattern:
    body = r"[\s\-_/]*".join(re.escape(p) for p in re.split(r"[\s\-_]+", form) if p)
    flags = 0 if len(re.sub(r"[^A-Za-z0-9]", "", form)) <= SHORT else re.I
    return re.compile(r"(?<![A-Za-z0-9+#.])" + body + r"(?![A-Za-z0-9+#&]|\.[A-Za-z])", flags)


@lru_cache(maxsize=4)
def _patterns(extra_mtime: float) -> tuple:
    return tuple((name, tuple(_pattern(f) for f in forms)) for name, (_, forms) in _lexicon(extra_mtime).items())


def find(text: str) -> dict:
    """{canonical: count} of every lexicon skill the text names."""
    text = text or ""
    out = {}
    for name, pats in _patterns(_mtime(_extra_path())):
        n = sum(len(p.findall(text)) for p in pats)
        if n:
            out[name] = n
    return out


def category_of(name: str) -> str | None:
    found = lexicon().get(name)
    return found[0] if found else None


def _sections(jd_text: str) -> list:
    """[(line, "req" | "pref")]: a heading line that reads like "Nice to have" switches to preferred until the next
    heading; a single line that says "preferred" / "a plus" counts as preferred on its own."""
    out, mode = [], "req"
    for line in (jd_text or "").splitlines():
        stripped = line.strip()
        is_heading = (stripped and len(stripped) <= HEADING_MAX and not BULLET.match(line)
                      and (stripped.endswith(":") or len(stripped.split()) <= 6))
        if is_heading and PREFERRED.search(stripped):
            mode = "pref"
        elif is_heading and REQUIRED.search(stripped):
            mode = "req"
        line_mode = "pref" if mode == "req" and PREFERRED.search(stripped) and not is_heading else mode
        out.append((line, line_mode))
    return out


def extract(jd_text: str) -> list:
    """The JD's hard skills, heaviest first: [{name, weight, need, count, forms, importance}]."""
    counts, need = {}, {}
    for line, mode in _sections(jd_text):
        for name, n in find(line).items():
            counts[name] = counts.get(name, 0) + n
            need[name] = need.get(name, False) or mode == "req"
    terms = []
    for name, count in counts.items():
        weight = round((2.0 if need[name] else 1.0) * (1 + 0.5 * min(count - 1, 2)), 2)
        terms.append({"name": name, "weight": weight, "need": need[name], "count": count,
                      "forms": list(lexicon()[name][1]),
                      "importance": "high" if weight >= 3 else "medium" if weight >= 2 else "low"})
    terms.sort(key=lambda t: (-t["weight"], t["name"].lower()))
    return terms


def _score_one(rid: str, label: str, found: dict, terms: list) -> dict:
    total = sum(t["weight"] for t in terms)
    matched = [t["name"] for t in terms if t["name"] in found]
    missing = [t["name"] for t in terms if t["name"] not in found]
    got = sum(t["weight"] for t in terms if t["name"] in found)
    pct = 100 if not total else round(100 * got / total)
    return {"id": rid, "label": label, "pct": pct, "matched": matched, "missing": missing}


def score(jd_text: str, resumes: list, title: str = "", company: str = "", jd_url: str | None = None,
          profile_years=None, extra_texts: dict | None = None) -> dict:
    """resumes: [{id, label, text, families}] -> the scores (see the module docstring)."""
    terms = extract(jd_text)
    found = {str(r["id"]): find(r.get("text") or "") for r in resumes}
    for key, text in (extra_texts or {}).items():
        found[key] = find(text or "")
    scores = [_score_one(str(r["id"]), str(r.get("label") or r["id"]), found[str(r["id"])], terms) for r in resumes]
    family = headline.for_job(title or "", "")

    def rank(s: dict) -> tuple:
        entry = next(r for r in resumes if str(r["id"]) == s["id"])
        fits = bool(family) and (family in (entry.get("families") or ())
                                 or headline.for_job(entry.get("label") or "", "") == family)
        return (s["pct"], fits)
    candidates = [s for s in scores if s["id"] != "tailored"]
    best = max(candidates, key=rank) if candidates else None
    jd_found = find(jd_text)
    names = set(jd_found)
    for d in found.values():
        names |= set(d)
    return {"vendor": "keyword", "vendor_label": "keyword", "modeled": False,
            "label": "Built-in keyword match (required skills count double)",
            "terms": terms, "scores": scores, "best_id": best["id"] if best else None, "found": found,
            "categories": {n: category_of(n) for n in sorted(names)}, "jd_found": jd_found}
