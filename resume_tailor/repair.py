"""Repair, don't regenerate (). After the full tailoring attempt, every later round touches only the
items that failed a check:

- mechanical(): fixes Python can make without Claude: drop a skill the person does not own, un-hyphenate a compound
  on a line that protrudes past the text column (Word and Google cannot break 'real-time image-based' early).
- plan(): turns the check failures into Items (one bullet / the summary / one skills line each) with the rule they
  break, plus the keywords an ATS repair may add and how many lines a too-long page must lose.
- apply(): merges Claude's replacements into a NEW content dict (only the planned items may change).
"""
import copy
import re
from dataclasses import dataclass, field

from . import checks, docxio

# compounds Word/Google will not break; the spaced or joined form lets the line wrap one word earlier.
HYPHEN_TABLE = {
    "real-time": "real time", "image-based": "image based", "end-to-end": "end to end", "large-scale": "large scale",
    "high-throughput": "high throughput", "low-latency": "low latency", "open-source": "open source",
    "data-driven": "data driven", "cross-functional": "cross functional", "multi-agent": "multi agent",
    "time-series": "time series", "gpu-accelerated": "GPU accelerated", "prompt-engineered": "prompt engineered",
    "audit-logged": "audit logged", "cross-encoder": "cross encoder", "pre-trained": "pretrained",
    "re-trained": "retrained", "re-ranking": "reranking", "fine-tuned": "fine tuned", "fine-tuning": "fine tuning",
    "production-grade": "production grade", "cloud-native": "cloud native", "human-in-the-loop": "human in the loop",
}
_HYPHEN_SUFFIX = re.compile(r"\b([A-Za-z]{2,})-(based|driven|level|ready|aware|scale|grade|backed|facing)\b")
_PRODUCT = re.compile(r"^(?:[A-Z0-9]{2,6}|[A-Za-z]+[A-Z0-9][A-Za-z0-9.+#-]*|[a-z]+[A-Z][A-Za-z0-9]*)$")  # GCP, IaC, vLLM
# lexicon categories whose terms are tools (safe to cut from a sentence); method words ('Optimization', 'Regression'
# under Data Science / Machine Learning) stay for a Claude repair
# lexicon.json categories whose entries are tools (product names), not generic words like "Optimization"
TOOL_CATEGORIES = frozenset({"Languages", "Deep Learning", "GenAI & Agentic AI", "LLM & Fine-tuning", "Vector Search",
                             "Data Engineering", "Databases", "Cloud", "MLOps & DevOps", "BI & Analytics",
                             "Backend & APIs", "Frontend"})
_BULLET_REF = re.compile(r"bullet (\d+) of role (\d+)")
_SKILL_REF = re.compile(r"skills(?: line)? '([^']+)'")
_QUOTED = re.compile(r"'([^']+)'")
_LINE_END = re.compile(r'"…(.*?)"')
MAX_ATS_BULLETS = 2


@dataclass(frozen=True)
class Item:
    kind: str                       # bullet | summary | skills
    where: str                      # "bullet 4 of role 2 (Brightline Health, USA)"
    current: str
    rules: tuple
    job: int = 0                    # 1-based (bullets)
    index: int = 0                  # 1-based (bullets, skills lines)

    @property
    def key(self) -> tuple:
        return (self.kind, self.job, self.index)


@dataclass(frozen=True)
class Plan:
    items: tuple = ()
    keywords: tuple = ()            # JD keywords the person owns that an ATS repair must add
    shorten_lines: int = 0          # lines the page must lose (check 1 / bottom margin)
    unrepairable: tuple = ()        # failures no item rewrite can fix (renderer, layout, fonts)
    notes: tuple = ()

    @property
    def empty(self) -> bool:
        return not self.items and not self.keywords


@dataclass(frozen=True)
class Mechanical:
    content: dict
    notes: tuple = ()

    @property
    def changed(self) -> bool:
        return bool(self.notes)


# ---------- text helpers ----------

def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\b(and|or|with|on|in|using|via|across)\s*([.,;])", r"\2", text)
    text = re.sub(r",\s*(and|or)\s+", r" \1 ", text) if text.count(",") == 1 else text
    text = re.sub(r"\s{2,}", " ", text).strip()
    return re.sub(r"\s+\.$", ".", text)


def term_variants(term: str) -> list:
    """A lexicon name and the ways it is written: 'Infrastructure as Code (IaC)' -> itself, 'Infrastructure as
    Code', 'IaC'."""
    out = [term.strip()]
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", term.strip())
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    return [v for v in out if v]


def _term_re(term: str) -> str:
    return "(?:" + "|".join(re.escape(v) for v in sorted(term_variants(term), key=len, reverse=True)) + ")"


def remove_term(text: str, term: str) -> str:
    """Drop `term` (any of its written forms) from prose, taking its joining word with it ("on GCP", ", GCP",
    "GCP and")."""
    t = _term_re(term)
    patterns = (rf",\s*{t}(?![\w-])", rf"(?<![\w-]){t}\s*,\s*", rf"(?<![\w-]){t}\s+(?:and|or)\s+",
                rf"\s+(?:and|or|on|in|with|using|via|across|to)\s+{t}(?![\w-])", rf"(?<![\w-]){t}(?![\w-])")
    for pat in patterns:
        new = re.sub(pat, " ", text, count=1, flags=re.I)
        if new != text:
            return _tidy(new)
    return text


def remove_from_items(items: list, term: str) -> list:
    keys = {checks.squash(v) for v in term_variants(term)}
    out = []
    for item in items:
        if checks.squash(item) in keys:
            continue
        new = re.sub(rf"(?<![\w-]){_term_re(term)}(?![\w-])", "", item, flags=re.I)
        new = _tidy(re.sub(r",\s*,", ",", new)).strip(" ,")
        wrapped = re.fullmatch(r"\((.*)\)", new)
        if wrapped:                            # 'NoSQL (Redis, Neo4j)' minus NoSQL -> 'Redis', 'Neo4j'
            out += docxio.split_items(wrapped.group(1))
        elif new:
            out.append(new)
    return out


def _keep_case(plain: str, found: str) -> str:
    return plain[0].upper() + plain[1:] if found[:1].isupper() else plain


def _clean_category(category: str, term: str) -> str:
    """'Cloud & IaC' -> 'Cloud' when IaC is the unknown term (a term can hide in the bold category name)."""
    keys = {checks.squash(v) for v in term_variants(term)}
    parts = re.split(r"(\s*(?:&|/|,|\band\b)\s*)", category)        # text, joiner, text, joiner, ...
    texts, joiners = parts[0::2], parts[1::2]
    keep = [i for i, t in enumerate(texts) if checks.squash(t) not in keys]
    if len(keep) == len(texts):
        return category
    out = ""
    for n, i in enumerate(keep):
        out += (joiners[i - 1] if n and i - 1 < len(joiners) else "") + texts[i]
    return re.sub(r"\s{2,}", " ", out).strip(" &/,")


def unhyphenate(text: str, protect: str = "") -> str:
    """Spaced/joined forms of the hyphenated compounds; a compound that appears in `protect` (the JD) is kept,
    because the ATS check wants the JD's exact wording."""
    out, guard = text, protect.lower()
    for compound, plain in HYPHEN_TABLE.items():
        if compound in guard or plain.lower() in guard:
            continue
        out = re.sub(rf"(?<![\w-]){re.escape(compound)}(?![\w-])", lambda m: _keep_case(plain, m.group(0)), out,
                     flags=re.I)
    return _HYPHEN_SUFFIX.sub(lambda m: m.group(0).replace("-", " ") if m.group(0).lower() not in guard else m.group(0),
                              out)


def named_in(term: str, text: str) -> bool:
    """True when `text` (the JD) names the term in any of its written forms."""
    return bool(text) and bool(re.search(rf"(?<![\w-]){_term_re(term)}(?![\w-])", text, re.I))


def words(text: str) -> int:
    return len(text.split())


# ---------- content addressing (1-based like the check messages) ----------

def _items_list(skill: dict) -> list:
    items = skill.get("items")
    return list(items) if isinstance(items, list) else docxio.split_items(str(items or ""))


def _elements(content: dict) -> list:
    """Every editable element as (kind, job, index, text)."""
    out = [("summary", 0, 0, content.get("summary") or "")]
    for i, s in enumerate(content.get("skills") or [], 1):
        out.append(("skills", 0, i, f"{s.get('category', '')}: {', '.join(_items_list(s))}"))
    for j, job in enumerate(content.get("jobs") or [], 1):
        for k, b in enumerate(job.get("bullets") or [], 1):
            out.append(("bullet", j, k, b))
    return out


def _find_element(content: dict, needle: str) -> tuple | None:
    """The element whose text contains `needle` (spaces, case and dashes ignored), longest match first."""
    key = checks.squash(needle)
    if len(key) < 8:
        return None
    hits = [e for e in _elements(content)
            if checks.squash(e[3]) and (key in checks.squash(e[3]) or checks.squash(e[3]) in key)]
    return max(hits, key=lambda e: len(e[3])) if hits else None


def _with_text(content: dict, kind: str, job: int, index: int, text: str) -> dict:
    """A new content dict with one element replaced (never mutates the input)."""
    new = copy.deepcopy(content)
    if kind == "summary":
        new["summary"] = text
    elif kind == "bullet":
        new["jobs"][job - 1]["bullets"][index - 1] = text
    return new


# ---------- mechanical fixes ----------

def _looks_like_product(term: str, category: str | None = None) -> bool:
    if category in TOOL_CATEGORIES:
        return True
    return any(" " not in v and _PRODUCT.match(v) for v in term_variants(term))


def drop_unknown_terms(content: dict, terms: list, prules: dict, categories: dict | None = None) -> Mechanical:
    """Unknown skills leave the skills lines always; prose loses them only when the term is a tool (its lexicon
    category, or a product-name shape such as GCP / IaC / vLLM) and the bullet stays inside the word range, so a
    generic word ('Optimization') is left for a Claude repair instead of a mangled sentence."""
    new, notes = copy.deepcopy(content), []
    lo = (prules.get("bullet_words") or (0, 10 ** 6))[0]
    categories = categories or {}
    for term in terms:
        for s in new.get("skills") or []:
            before = _items_list(s)
            after = remove_from_items(before, term)
            if after != before and after:
                s["items"] = after
                notes.append(f"dropped '{term}' from skills '{s.get('category', '')}'")
            category = _clean_category(str(s.get("category") or ""), term)
            if category and category != s.get("category"):
                notes.append(f"renamed skills '{s.get('category', '')}' to '{category}'")
                s["category"] = category
        added = list(new.get("added_from_jd") or [])
        kept = [a for a in added if not re.search(rf"(?<![\w-]){_term_re(term)}(?![\w-])", str(a), re.I)]
        if kept != added:
            new["added_from_jd"] = kept
        if not _looks_like_product(term, categories.get(term)):
            continue
        for j, job in enumerate(new.get("jobs") or [], 1):
            for k, b in enumerate(job.get("bullets") or [], 1):
                stripped = remove_term(b, term)
                if stripped != b and words(stripped) >= lo:
                    job["bullets"][k - 1] = stripped
                    notes.append(f"dropped '{term}' from bullet {k} of role {j}")
        summary = remove_term(new.get("summary") or "", term)
        if summary != new.get("summary"):
            new["summary"] = summary
            notes.append(f"dropped '{term}' from the summary")
        headline = new.get("headline") or ""
        if re.search(rf"(?<![\w-]){_term_re(term)}(?![\w-])", headline, re.I):
            new["headline"] = _tidy(re.sub(rf"(?<![\w-]){_term_re(term)}(?![\w-])", "", headline, flags=re.I))
            notes.append(f"dropped '{term}' from the headline")
    return Mechanical(new, tuple(notes))


def fix_protruding(content: dict, line_texts: list, prules: dict, jd_text: str = "") -> Mechanical:
    """For each line that runs past the text column: un-hyphenate the element it belongs to (table + '-based'
    style suffixes) so the renderer can break the line one word earlier."""
    hi = (prules.get("bullet_words") or (0, 10 ** 6))[1]
    new, notes = content, []
    for text in line_texts:
        hit = _find_element(new, text)
        if hit is None:
            continue
        kind, job, index, current = hit
        if kind == "skills":
            continue
        fixed = unhyphenate(current, jd_text)
        if fixed == current:
            continue
        if kind == "bullet" and words(fixed) > hi:
            notes_text = f"bullet {index} of role {job}: un-hyphenating would reach {words(fixed)} words; left to Claude"
            notes.append(notes_text)
            continue
        new = _with_text(new, kind, job, index, fixed)
        where = f"bullet {index} of role {job}" if kind == "bullet" else kind
        notes.append(f"{where}: replaced hyphenated compounds so the line can break earlier")
    return Mechanical(new, tuple(n for n in notes if "left to Claude" not in n)) if new is not content \
        else Mechanical(content, ())


def mechanical(content: dict, report: checks.Report, prules: dict, jd_text: str = "") -> Mechanical:
    """Every free fix the report allows, in one pass. Returns the new content and what was done."""
    honesty = report.info.get("honesty") or {}
    visual = report.info.get("visual") or {}
    out = Mechanical(content, ())
    unknown = [t for t in honesty.get("unknown_terms") or [] if not named_in(t, jd_text)]   # never drop a JD skill
    if unknown:
        step = drop_unknown_terms(out.content, unknown, prules, honesty.get("unknown_categories") or {})
        out = Mechanical(step.content, out.notes + step.notes)
    lines = list(visual.get("protruding") or [])
    for f in report.failures:
        if f.check == 8 and f.name == "right_margin":
            lines += _LINE_END.findall(f.message)
    if lines:
        step = fix_protruding(out.content, lines, prules, jd_text)
        out = Mechanical(step.content, out.notes + step.notes)
    return out


# ---------- the repair plan ----------

def _bullet_item(content: dict, j: int, k: int, rule: str) -> Item | None:
    jobs = content.get("jobs") or []
    if not (1 <= j <= len(jobs)) or not (1 <= k <= len(jobs[j - 1].get("bullets") or [])):
        return None
    company = jobs[j - 1].get("company", "")
    return Item("bullet", f"bullet {k} of role {j} ({company})", jobs[j - 1]["bullets"][k - 1], (rule,), j, k)


def _skills_item(content: dict, index: int, rule: str) -> Item | None:
    skills = content.get("skills") or []
    if not (1 <= index <= len(skills)):
        return None
    s = skills[index - 1]
    return Item("skills", f"skills line {index} ('{s.get('category', '')}')",
                f"{s.get('category', '')}: {', '.join(_items_list(s))}", (rule,), 0, index)


def _summary_item(content: dict, rule: str) -> Item:
    return Item("summary", "the summary", content.get("summary") or "", (rule,))


def _skills_index(content: dict, category: str) -> int | None:
    key = checks.squash(category)
    for i, s in enumerate(content.get("skills") or [], 1):
        if checks.squash(s.get("category", "")) == key:
            return i
    return None


def _merge(items: dict, item: Item | None) -> None:
    if item is None:
        return
    old = items.get(item.key)
    items[item.key] = item if old is None else Item(item.kind, item.where, item.current, old.rules + item.rules,
                                                    item.job, item.index)


def _items_for_terms(content: dict, terms: list, rule_fmt: str, jobs_only: int | None = None) -> list:
    out = []
    for term in terms:
        for kind, j, k, text in _elements(content):
            if jobs_only is not None and (kind != "bullet" or j != jobs_only):
                continue
            if re.search(rf"(?<![\w-]){_term_re(term)}(?![\w-])", text, re.I):
                rule = rule_fmt.format(term=term)
                if kind == "bullet":
                    out.append(_bullet_item(content, j, k, rule))
                elif kind == "summary":
                    out.append(_summary_item(content, rule))
                elif kind == "skills":
                    out.append(_skills_item(content, k, rule))
    return [i for i in out if i is not None]


def _shorten_candidates(content: dict, report: checks.Report, base_measure, need: int) -> list:
    """Elements to shorten when the page is too long: anything rendering longer than the base's slot first
    (3-line bullets, a skills line that wrapped, a longer summary), then the longest bullets by characters."""
    fit = report.info.get("fit") or {}
    out, seen = [], set()
    rule = "the page is {n} line(s) too long: make this {lines} line(s) shorter (fewer characters, same facts)"
    base_summary = getattr(base_measure, "summary_lines", 0) or 0
    got_summary = int(fit.get("summary_lines") or 0)
    if got_summary and base_summary and got_summary > base_summary:      # the one element that can lose a line
        out.append(_summary_item(content, rule.format(n=need, lines=got_summary - base_summary)
                                 + f" (it renders {got_summary} lines; the base's summary renders {base_summary})"))
    for ref, n in sorted((fit.get("bullet_lines") or {}).items(), key=lambda kv: -kv[1]):
        if n > 2:
            j, k = (int(x) for x in ref.split("."))
            out.append(_bullet_item(content, j, k, rule.format(n=need, lines=n - 2)))
            seen.add(("bullet", j, k))
    for category, n in (fit.get("skill_lines") or {}).items():
        base_n = (base_measure.skill_lines or {}).get(category, 1)
        if n > base_n:
            idx = _skills_index(content, category)
            if idx:
                out.append(_skills_item(content, idx, rule.format(n=need, lines=n - base_n)))
                seen.add(("skills", 0, idx))
    longest = sorted(((len(b), j, k) for j, job in enumerate(content.get("jobs") or [], 1)
                      for k, b in enumerate(job.get("bullets") or [], 1)), reverse=True)
    if not any(i for i in out if i and i.kind == "summary"):
        out.append(_summary_item(content, rule.format(n=need, lines=1)
                                 + " (a shorter summary is the safest way to lose a line)"))
    for _, j, k in longest:
        if len([i for i in out if i]) >= need + 2:
            break
        if ("bullet", j, k) not in seen:
            out.append(_bullet_item(content, j, k, rule.format(n=need, lines=1)))
            seen.add(("bullet", j, k))
    return [i for i in out if i is not None]


def allowed_keywords(scores: dict | None, known_skills: list, base_ids: list, jd_skills: bool = False) -> set:
    """Terms an ATS repair may add: known_skills.txt plus everything the lexicon found in any base resume, and every
    JD term when the person's rules mirror the JD's hard skills."""
    found = (scores or {}).get("found") or {}
    out = set(known_skills) | set(found.get("known") or {})
    if jd_skills:
        out |= set((scores or {}).get("jd_found") or {}) | {t["name"] for t in (scores or {}).get("terms") or []}
    for bid in base_ids:
        out |= set(found.get(bid) or {})
    return out


def plan(report: checks.Report, content: dict, base_measure, prules: dict, allowed: set) -> Plan:
    items: dict = {}
    keywords, unrepairable, notes = set(), [], []
    shorten = 0
    ats = report.info.get("ats") or {}
    honesty = report.info.get("honesty") or {}
    for f in report.failures:
        m = _BULLET_REF.search(f.message)
        sm = _SKILL_REF.search(f.message)
        if f.name == "period":
            for bm in _BULLET_REF.finditer(f.message):
                _merge(items, _bullet_item(content, int(bm.group(2)), int(bm.group(1)), f.message))
        elif f.name in ("banned", "em_dash", "em_dash_chain") and sm:
            idx = _skills_index(content, sm.group(1))
            _merge(items, _skills_item(content, idx, f.message) if idx else None)
        elif f.name in ("widow", "short_bullet", "locate", "bullet_words", "banned", "em_dash", "em_dash_chain",
                        "numbers") and m:
            _merge(items, _bullet_item(content, int(m.group(2)), int(m.group(1)), f.message))
        elif f.name in ("banned", "em_dash", "em_dash_chain", "summary_words", "summary_sentences",
                        "summary_start", "summary_title", "summary_contains") or (f.name == "numbers" and not m):
            if f.name == "numbers":
                for hit in _items_for_terms(content, [n.split("'")[1] for n in honesty.get("bad_numbers") or []
                                                      if "'" in n], "the number '{term}' is not in the base resume "
                                                                     "or fact bank: remove or replace it"):
                    _merge(items, hit)
            else:
                _merge(items, _summary_item(content, f.message))
        elif f.name == "skill_lines":
            sm = _SKILL_REF.search(f.message)
            idx = _skills_index(content, sm.group(1)) if sm else None
            _merge(items, _skills_item(content, idx, f.message) if idx else None)
        elif f.name == "skill_total":
            for i in range(1, len(content.get("skills") or []) + 1):
                _merge(items, _skills_item(content, i, f.message + "; drop a term from the longest lines"))
        elif f.name == "unknown_skills":
            hits = _items_for_terms(content, _QUOTED.findall(f.message),
                                    "'{term}' is not a skill this person has: remove it, keep the rest")
            if not hits:                       # written in a form we cannot locate: let Claude find it
                hits = [_summary_item(content, f.message + " (find where it is written and remove it)")]
                hits += [_skills_item(content, i, f.message) for i in range(1, len(content.get("skills") or []) + 1)]
            for hit in hits:
                _merge(items, hit)
        elif f.name == "era":
            jm = re.match(r"(.+?) bullets mention", f.message)
            terms = [t.strip() for t in f.message.split("that role:")[-1].split(";")[0].split(",")]
            job_idx = next((j for j, job in enumerate(content.get("jobs") or [], 1)
                            if jm and job.get("company", "").startswith(jm.group(1))), None)
            for hit in _items_for_terms(content, terms, "'{term}' did not exist in this role's era: use classic "
                                                        "ML / data engineering wording", job_idx):
                _merge(items, hit)
        elif f.name in ("verbs", "verbs_once"):
            company = f.message.split(" bullets")[0].split(" uses")[0]
            for j, job in enumerate(content.get("jobs") or [], 1):
                if job.get("company", "").split(",")[0].strip() == company.split(",")[0].strip():
                    for k in range(1, len(job.get("bullets") or []) + 1):
                        _merge(items, _bullet_item(content, j, k, f.message))
        elif f.name in ("ats_score", "ats_coverage"):
            wanted = list(ats.get("missing") or []) + list(ats.get("uncovered_known") or [])
            had = (set(ats.get("missing") or []) - set(ats["base_missing"])        # the base matched it: owned
                   if "base_missing" in ats else set())
            keywords |= {t for t in wanted if t in allowed or t in had}
            if not keywords:
                notes.append(f"{f.message[:120]}: no missing keyword the person owns, nothing to add")
        elif f.name in ("pages", "bottom_margin"):
            shorten = max(shorten, int(report.info.get("overflow_lines") or 1))
        elif f.name == "right_margin":
            for text in _LINE_END.findall(f.message):
                hit = _find_element(content, text)
                if hit and hit[0] == "bullet":
                    _merge(items, _bullet_item(content, hit[1], hit[2], "this bullet's line runs past the page: "
                                                                        "reword so it breaks earlier"))
                elif hit and hit[0] == "summary":
                    _merge(items, _summary_item(content, f.message))
        else:
            unrepairable.append(f.message)
    if shorten:
        for item in _shorten_candidates(content, report, base_measure, shorten):
            _merge(items, item)
    if keywords:
        for i in range(1, len(content.get("skills") or []) + 1):
            _merge(items, _skills_item(content, i, "add the JD keywords listed below where they fit"))
    return Plan(tuple(items.values()), tuple(sorted(keywords)), shorten, tuple(unrepairable), tuple(notes))


# ---------- applying Claude's replacements ----------

def apply(content: dict, output: dict, plan_: Plan) -> tuple:
    """(new content, notes). Only planned bullets/skills/summary change; with ATS keywords up to MAX_ATS_BULLETS
    unplanned bullets may change too."""
    new, notes = copy.deepcopy(content), []
    planned = {i.key for i in plan_.items}
    free = MAX_ATS_BULLETS if plan_.keywords else 0
    jobs = new.get("jobs") or []
    for rep in output.get("bullets") or []:
        try:
            j, k, text = int(rep["job"]), int(rep["index"]), docxio._clean(str(rep["text"]))
        except (KeyError, TypeError, ValueError):
            notes.append(f"ignored a malformed bullet replacement: {str(rep)[:80]}")
            continue
        if not text or not (1 <= j <= len(jobs)) or not (1 <= k <= len(jobs[j - 1].get("bullets") or [])):
            notes.append(f"ignored bullet {k} of role {j}: no such bullet")
            continue
        if ("bullet", j, k) not in planned:
            if free <= 0:
                notes.append(f"ignored bullet {k} of role {j}: it was not up for repair")
                continue
            free -= 1
        jobs[j - 1]["bullets"][k - 1] = text
        notes.append(f"bullet {k} of role {j} replaced")
    skills = new.get("skills") or []
    for rep in output.get("skills") or []:
        try:
            idx = int(rep["index"])
            category = docxio._clean(str(rep.get("category") or ""))
            items = [docxio._clean(str(x)) for x in (rep.get("items") or []) if str(x).strip()]
        except (KeyError, TypeError, ValueError):
            notes.append(f"ignored a malformed skills replacement: {str(rep)[:80]}")
            continue
        if ("skills", 0, idx) not in planned or not (1 <= idx <= len(skills)) or not items:
            notes.append(f"ignored skills line {idx}: not up for repair or empty")
            continue
        skills[idx - 1] = {"category": category or skills[idx - 1].get("category"), "items": items}
        notes.append(f"skills line {idx} replaced")
    summary = output.get("summary")
    if summary and ("summary", 0, 0) in planned:
        new["summary"] = docxio._clean(str(summary))
        notes.append("summary replaced")
    elif summary:
        notes.append("ignored the summary: it was not up for repair")
    return new, notes


# ---------- "Fix with note": one repair focused on what the user's note names ----------

def _element_item(content: dict, kind: str, job: int, index: int, rule: str) -> Item | None:
    if kind == "bullet":
        return _bullet_item(content, job, index, rule)
    if kind == "skills":
        return _skills_item(content, index, rule)
    return _summary_item(content, rule)


def note_plan(content: dict, note: str) -> Plan:
    """Every element may be returned (Python cannot read the note); the prompt tells Claude to return only the
    ones the note names. Nothing runs after the note round, so no focus is kept."""
    rule = f"the user's note: {note}"
    items = [_element_item(content, kind, j, k, rule) for kind, j, k, _ in _elements(content)]
    return Plan(tuple(i for i in items if i is not None))
