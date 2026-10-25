"""The tailoring checks a profile turns on in rules.toml, with LOCKED / DROP ORDER / anchors / spares from
skillrules (rules.toml and facts.md Part 3).

- summary_structure: sentence 1 opens "<title> with <years> <verb>" and names only summary_industries after
  "across"; at most N numbers besides the years; at least N employers named; sentences 2 and 3 not the same shape;
  no sentence copied from a bullet.
- skills_add_first: a removed skill must be LOCKED-free, not named by the JD, from that row's DROP ORDER, first out
  first, and only in a row that gained a JD skill; a JD skill the person has that is missing while a removable non-JD
  skill is still in its row fails. "Inside each row: JD skills first, then the rest in base order" is not a check:
  Python puts every row in that order before the page is made (jd_first, as the title under the name is locked).
- spare_swaps: Python reads the swaps from the page itself (which spare bullets went in, which base bullets went out,
  per job): own job only, no anchor out, a "variant of X" spare only in place of X, at most N per job, and no number a
  spare states is stated by another bullet too. The answer's `swaps` give each tag its reason.
Every message names "bullet k of role j", "skills '<row>'" or "summary" so a repair round finds the item."""
import re
from collections import Counter

from . import checks, docxio, rules, skillrules
from .checks import Failure

SPARE_MIN = 0.45              # a page bullet reads as a source (base or spare bullet) at this similarity or more
CROSS_MIN = 0.6               # a page bullet this close to another job's spare is that spare
_SAYS = {"cost": ("spend", "spent", "cost")}        # an anchor / target word the bullet says another way
_RANGE_NUM = re.compile(r"(?<![\w.$\-–])(\$?\d[\d,]*(?:\.\d+)?)([%KkB+x]?)(?![\d\-–])(?:\s+([A-Za-z]+))?")
_COUNTED = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?[%KkB+]?(?:\s*[-–]\s*\$?\d[\d,]*(?:\.\d+)?[%KkB+]?)?")


def run(inp, tstruct) -> tuple:
    """(failures, info) for the rules the person's profile turns on; ([], {}) when none is on."""
    prules = checks.job_rules(inp)
    if not any(prules.get(k) for k in ("summary_structure", "skills_add_first", "spare_swaps")):
        return [], {}
    sr = skillrules.for_rules(prules, getattr(inp, "fact_bank_all", "") or "")
    answer = getattr(inp, "answer", None) or {}
    out, info = [], {}
    if prules.get("summary_structure"):
        out += summary_problems(inp, tstruct, prules, sr)
    if prules.get("skills_add_first"):
        fails, info["skills"] = skill_problems(inp, tstruct, sr)
        out += fails
    if prules.get("spare_swaps"):
        fails, info["swaps"] = swap_problems(inp, tstruct, sr, answer)
        out += fails
    return out, info


# ---------- summary_structure ----------

def _short(company: str) -> str:
    return (company or "").split(",")[0].strip()


def employer_named(company: str, text: str) -> bool:
    """'Lumen Freight, USA' is named by "Lumen Freight"; an all-caps first word names it too ("ACME" for 'ACME Labs')."""
    short = _short(company)
    first = short.split()[0] if short else ""
    return bool(short) and (checks._has(short, text) or (first.isupper() and checks._has(first, text)))


def summary_problems(inp, tstruct, prules: dict, sr) -> list:
    spec = prules["summary_structure"]
    summary = tstruct.summary or ""
    sentences = checks._sentence_list(summary)
    out = []
    start = rules.summary_start(prules, inp.base_id)
    want = f"{start} {spec['verb']}" if start and spec.get("verb") else None
    if want and not summary.startswith(want):
        out.append(Failure(7, "summary_structure", f"the summary's sentence 1 must open \"{want} <the 2 or 3 kinds "
                                                   f"of work this JD cares about most> across <industries>\""))
    first = sentences[0] if sentences else ""
    if sr.industries and " across " in first:
        named = [x.strip().lower().removeprefix("the ") for x in
                 re.split(r",\s*|\s+and\s+", first.split(" across ", 1)[1].rstrip(". ")) if x.strip()]
        wrong = [x for x in named if x not in sr.industries]
        if wrong:
            out.append(Failure(7, "summary_structure", f"the summary names {', '.join(repr(w) for w in wrong)} "
                                                       f"after \"across\"; industries only: "
                                                       + ", ".join(sr.industries)))
    years = prules.get("summary_years") or ""
    counted = _COUNTED.findall(summary.replace(years, " ", 1) if years else summary)
    cap = spec.get("max_numbers")
    if cap is not None and len(counted) > cap:
        out.append(Failure(7, "summary_structure", f"the summary states {len(counted)} numbers ({', '.join(counted)});"
                                                   f" at most {cap} besides \"{years}\""))
    need = spec.get("min_employers") or 0
    named_jobs = [j["company"] for j in inp.base_structure.jobs if employer_named(j["company"], summary)]
    if len(named_jobs) < need:
        out.append(Failure(7, "summary_structure", f"the summary names {len(named_jobs)} employer(s); at least "
                                                   f"{need} of " + ", ".join(_short(j['company'])
                                                                            for j in inp.base_structure.jobs)))
    if len(sentences) >= 3 and _opening(sentences[1]) == _opening(sentences[2]):
        out.append(Failure(7, "summary_structure", f"summary sentences 2 and 3 open the same way "
                                                   f"(\"{' '.join(sentences[1].split()[:2])} ...\"); give them "
                                                   f"different shapes"))
    bullets = {checks.squash(s) for job in tstruct.jobs for b in job["bullets"] for s in checks._sentence_list(b)}
    copied = [s for s in sentences[1:] if checks.squash(s) in bullets]
    if copied:
        out.append(Failure(7, "summary_structure", "the summary repeats a bullet's sentence word for word: \""
                                                   + copied[0][:80] + "\""))
    return out


def _opening(sentence: str) -> tuple:
    """The sentence's skeleton: "At <employer>, <verb>" is one shape whatever the employer ("At X, built..." twice
    is what the prompt forbids); otherwise its first two words."""
    m = re.match(r"^(At|In|Before|After)\s+[^,]{1,40},\s*(\w+)", sentence)
    if m:
        return (m.group(1).lower(), "<employer>", m.group(2).lower())
    return tuple(w.lower().strip(",") for w in sentence.split()[:2])


# ---------- skills_add_first ----------

def _names(item: str) -> list:
    """'Amazon Web Services (AWS)' -> [it, 'Amazon Web Services', 'AWS']; 'LoRA/PEFT' -> [it, 'LoRA', 'PEFT']."""
    head, _, inner = item.partition("(")
    out = [item.strip(), head.strip()] + [x.strip() for x in inner.rstrip(")").split(",") if x.strip()]
    out += [p.strip() for x in list(out) for p in x.split("/") if "/" in x]
    return [n for n in dict.fromkeys(out) if len(n) >= 2]


def named_by_jd(item: str, jd: str, jd_found: set) -> bool:
    return any(checks._has(n, jd) or checks.squash(n) in jd_found for n in _names(item))


def _split(row_items: str) -> list:
    return [x.strip() for x in docxio.split_items(row_items) if x.strip()]


def _row_of(term: str, rows: list, scores: dict) -> int | None:
    """The row a JD term belongs in: the base row that lists it, else the row whose skills share its lexicon
    category most (ats.score scores each row as an extra text, 'skills_row_<i>')."""
    key = checks.squash(term)
    for i, row in enumerate(rows):
        if any(checks.squash(n) == key for item in row for n in _names(item)):
            return i
    categories = (scores or {}).get("categories") or {}
    cat = categories.get(term)
    if not cat:
        return None
    found = (scores or {}).get("found") or {}
    counts = Counter({i: sum(1 for n in found.get(f"skills_row_{i}") or {} if categories.get(n) == cat)
                      for i in range(len(rows))})
    best = counts.most_common(2)
    if not best or best[0][1] == 0 or (len(best) > 1 and best[0][1] == best[1][1]):
        return None
    return best[0][0]


def skill_problems(inp, tstruct, sr) -> tuple:
    jd = inp.jd_text or ""
    scores = inp.scores or {}
    jd_found = {checks.squash(n) for n in scores.get("jd_found") or {}}
    locked = {checks.squash(x) for x in sr.locked}
    out, info = [], {"added": {}, "removed": {}, "missing_jd": []}
    masters = [_split(b.items) for b in inp.base_structure.skills]
    rows_now = []
    for base, master, row in zip(inp.base_structure.skills, masters, tstruct.skills):
        now = _split(row.items)
        rows_now.append(now)
        name = base.category
        m_keys = {checks.squash(x) for x in master}
        n_keys = {checks.squash(x) for x in now} | {checks.squash(x) for x in master if _kept(x, now)}
        removed = [x for x in master if checks.squash(x) not in n_keys]
        added = [x for x in now if checks.squash(x) not in m_keys]
        if added:
            info["added"][name] = added
        if removed:
            info["removed"][name] = removed
        drop = [d for d in sr.drop_order.get(name, ()) if not named_by_jd(d, jd, jd_found)
                and checks.squash(d) not in locked]
        drop_keys = [checks.squash(d) for d in drop]
        for x in removed:
            out += _removal_problem(name, x, removed, drop, drop_keys, n_keys, locked, jd, jd_found)
        if removed and not added:
            out.append(Failure(4, "skills_add_first", f"skills '{name}' removed {', '.join(removed)} but added no "
                                                      f"JD skill; a row changes only to make room: put them back"))
    for term in _missing_jd(inp, scores):
        info["missing_jd"].append(term)
        i = _row_of(term, masters, scores)
        if i is None or i >= len(rows_now):
            continue
        name = inp.base_structure.skills[i].category
        have = {checks.squash(x) for x in rows_now[i]}
        free = [d for d in sr.drop_order.get(name, ()) if checks.squash(d) in have
                and not named_by_jd(d, jd, jd_found) and checks.squash(d) not in locked]
        if free:
            out.append(Failure(4, "skills_add_first", f"skills '{name}' is missing '{term}', a JD skill the person "
                                                      f"has, while '{free[0]}' (DROP ORDER, not in the JD) is still "
                                                      f"in the row: add '{term}' first, then drop '{free[0]}' only "
                                                      f"if the row overflows"))
    return out, info


def _removal_problem(name, x, removed, drop, drop_keys, n_keys, locked, jd, jd_found) -> list:
    key = checks.squash(x)
    if key in locked:
        return [Failure(4, "skills_add_first", f"skills '{name}' removed the LOCKED skill '{x}'; locked skills are "
                                               f"never removed: put it back")]
    if named_by_jd(x, jd, jd_found):
        return [Failure(4, "skills_add_first", f"skills '{name}' removed '{x}', which the JD names: put it back")]
    if drop_keys and key not in drop_keys:
        return [Failure(4, "skills_add_first", f"skills '{name}' removed '{x}', which is not in this row's DROP ORDER "
                                               f"({', '.join(drop)}): put it back")]
    if key in drop_keys and drop_keys.index(key) >= len(removed):    # the removed ones are the DROP ORDER's first
        first = next((d for d in drop if checks.squash(d) in n_keys), None)
        if first:
            return [Failure(4, "skills_add_first", f"skills '{name}' removed '{x}' while '{first}' is still in the "
                                                   f"row; the DROP ORDER takes '{first}' out first")]
    return []


def jd_first(content: dict, base_content: dict, jd: str, jd_found) -> dict:
    """A new content whose every skills row lists the JD's skills first (in the answer's order), then the rest in the
    base's order (skills_add_first). Rows, names and items are unchanged otherwise."""
    found = {checks.squash(n) for n in jd_found or ()}
    rows = []
    for n, row in enumerate(content.get("skills") or []):
        master = (base_content.get("skills") or [])[n] if n < len(base_content.get("skills") or []) else {}
        rank = {checks.squash(x): i for i, x in enumerate(_row_items(master))}
        items = _row_items(row)
        first = [x for x in items if named_by_jd(x, jd, found)]
        rest = sorted((x for x in items if not named_by_jd(x, jd, found)),
                      key=lambda x: rank.get(checks.squash(x), len(rank)))
        ordered = first + rest
        rows.append({**row, "items": ordered if isinstance(row.get("items"), list) else ", ".join(ordered)})
    return {**content, "skills": rows}


def drop_by_order(content: dict, base_content: dict, sr, jd: str, jd_found) -> dict:
    """A new content whose skills rows lose base items only by their row's DROP ORDER (skills_add_first):
    a row that took out k base items to make room loses the first k free DROP ORDER items instead (never a LOCKED
    one, never one the JD names), and the rest come back; a row that added nothing gets every base item back. Only
    base items move, so nothing is claimed that the base does not say."""
    found = {checks.squash(n) for n in jd_found or ()}
    locked = {checks.squash(x) for x in sr.locked}
    masters = base_content.get("skills") or []
    rows = []
    for n, row in enumerate(content.get("skills") or []):
        if n >= len(masters):
            rows.append(row)
            continue
        master, items = _row_items(masters[n]), _row_items(row)
        m_keys = {checks.squash(x) for x in master}
        n_keys = {checks.squash(x) for x in master if _kept(x, items)}       # kept, maybe under another name
        added = [x for x in items if checks.squash(x) not in m_keys]
        k = sum(checks.squash(x) not in n_keys for x in master) if added else 0
        free = [checks.squash(d) for d in sr.drop_order.get((masters[n] or {}).get("category") or "", ())
                if checks.squash(d) in m_keys and checks.squash(d) not in locked and not named_by_jd(d, jd, found)]
        out = set(free[:k])
        if {checks.squash(x) for x in master if checks.squash(x) not in n_keys} == out:
            rows.append(row)
            continue
        kept = [x for x in items if checks.squash(x) not in out]
        back = [x for x in master if checks.squash(x) not in n_keys and checks.squash(x) not in out]
        ordered = kept + back
        rows.append({**row, "items": ordered if isinstance(row.get("items"), list) else ", ".join(ordered)})
    return {**content, "skills": rows}


def _words(item: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z0-9+#]+", item.lower().replace("(", " ").replace(")", " ")) if w)


def same_skill(a: str, b: str) -> bool:
    """'Kafka' and 'Apache Kafka', 'Claude' and 'Claude API': one name's words are all in the other's."""
    x, y = _words(a), _words(b)
    return bool(x and y) and x != y and (x < y or y < x)


def _kept(x: str, items: list) -> bool:
    """A base item still on the page, as itself or under another name for the same skill."""
    return checks.squash(x) in {checks.squash(i) for i in items} or any(same_skill(x, i) for i in items)


def merge_same(content: dict, base_content: dict, jd: str, jd_found) -> dict:
    """A new content where an added skill that is a base item under another name is not listed twice: the JD's
    fuller name takes the base item's place ('Kafka' becomes 'Apache Kafka'), else the added one goes ('Claude'
    beside 'Claude API'). Two base items are never merged (a base's own 'SQL' and 'SQL Server' stay)."""
    found = {checks.squash(n) for n in jd_found or ()}
    masters = base_content.get("skills") or []
    rows = []
    for n, row in enumerate(content.get("skills") or []):
        master = {checks.squash(x) for x in _row_items(masters[n])} if n < len(masters) else set()
        items = _row_items(row)
        out = list(items)
        for added in [x for x in items if checks.squash(x) not in master]:
            twin = next((x for x in out if checks.squash(x) in master and same_skill(added, x)), None)
            if twin is None or added not in out:
                continue
            if named_by_jd(added, jd, found) and len(_words(added)) > len(_words(twin)):     # the JD's fuller name
                out = [added if x == twin else x for x in out if x != added]
            else:
                out = [x for x in out if x != added]
        if out == items:
            rows.append(row)
            continue
        rows.append({**row, "items": out if isinstance(row.get("items"), list) else ", ".join(out)})
    return {**content, "skills": rows}


def _row_items(row: dict) -> list:
    items = (row or {}).get("items") or []
    return [str(x).strip() for x in (items if isinstance(items, list) else docxio.split_items(items)) if str(x).strip()]


def _missing_jd(inp, scores: dict) -> list:
    """JD terms the person has (any base resume or known skills) that are nowhere on the tailored page."""
    found = scores.get("found") or {}
    owned = set()
    for key, d in found.items():
        if key not in ("tailored", "jd") and not key.startswith(("era_job_", "skills_row_")):
            owned |= set(d)
    known = {checks.squash(k) for k in inp.known_skills}
    page = set(found.get("tailored") or {})
    return sorted({t["name"] for t in scores.get("terms") or []
                   if (t["name"] in owned or checks.squash(t["name"]) in known) and t["name"] not in page})


# ---------- spare_swaps ----------

def _stems(text: str) -> set:
    return {checks._stem(w) for w in checks._content_words(text)}


def similarity(source: str, target: str) -> float:
    """Share of the source's content words (stemmed) on the target, averaged with the share of its numbers."""
    words, have = _stems(source), _stems(target)
    if not words:
        return 0.0
    share = sum(1 for w in words if checks._known(w, have)) / len(words)
    nums = checks._numbers(source)
    return share if not nums else (share + len(nums & checks._numbers(target)) / len(nums)) / 2


def _strip_employer(phrase: str, company: str) -> str:
    short = _short(company)
    for head in (short, short.split()[0] if short else ""):
        if head and phrase.lower().startswith(head.lower() + " "):
            return phrase[len(head):].strip()
    return phrase


def phrase_bullet(phrase: str, bullets: list) -> int | None:
    """The base bullet a short name points at ("forecasting", "drift", "cost"): most of its words, first wins."""
    words = _stems(phrase.replace('"', " ")) | set(re.findall(r"\d+", phrase))
    best, at = 0, None
    for k, text in enumerate(bullets):
        have = _stems(text) | set(re.findall(r"\d+", text))
        score = sum(1 for w in words if checks._known(w, have) or any(checks._has(s, text) for s in _SAYS.get(w, ())))
        if score > best:
            best, at = score, k
    return at


def _anchors_of(sr, company: str) -> list:
    short = _short(company)
    first = short.split()[0] if short else ""
    return [a for a in sr.anchors if short and (a.lower().startswith(short.lower() + " ")
                                                or a.lower().startswith(first.lower() + " "))]


def assign(sources: list, page: list) -> dict:
    """Greedy one-to-one match of page bullets to sources ((kind, key, text)): page index -> (source, similarity)."""
    pairs = sorted(((similarity(src[2], text), t, s) for t, text in enumerate(page) for s, src in enumerate(sources)),
                   key=lambda p: (-p[0], p[1], p[2]))
    out, used = {}, set()
    for score, t, s in pairs:
        if t in out or s in used or score < SPARE_MIN:
            continue
        out[t], used = (sources[s], score), used | {s}
    return out


def _key(match) -> str:
    number, suffix, word = match.group(1).lstrip("$").replace(",", ""), match.group(2), match.group(3) or ""
    return number + suffix if suffix else f"{number} {checks._stem(word.lower())}"


def number_keys(text: str) -> set:
    """'70% to 88%' -> {'70%', '88%'}; '9 API tools' -> {'9 api'}; ranges like 4-6 and $2-4K are left out."""
    return {_key(m) for m in _RANGE_NUM.finditer(text or "")}


def swap_problems(inp, tstruct, sr, answer: dict) -> tuple:
    declared = {str(s.get("spare_id") or "").strip(): s for s in answer.get("swaps") or [] if isinstance(s, dict)}
    out, swaps, placed = [], [], {}
    for j, (base_job, job) in enumerate(zip(inp.base_structure.jobs, tstruct.jobs)):
        fails, job_swaps, job_placed = _job_swaps(sr, j, base_job, job["bullets"], declared)
        out += fails
        swaps += job_swaps
        placed.update(job_placed)
    out += _repeated_numbers(tstruct, placed)
    found = {s["spare_id"] for s in swaps}
    return out, {"swaps": swaps, "declared_not_found": sorted(k for k in declared if k and k not in found)}


def _job_swaps(sr, j: int, base_job: dict, page: list, declared: dict) -> tuple:
    company, bullets = base_job["company"], base_job["bullets"]
    sources = [("base", k, b) for k, b in enumerate(bullets)] + [("spare", s, s.text) for s in sr.spares_of(company)]
    got = assign(sources, page)
    ins = [(t, src[1]) for t, (src, _) in sorted(got.items()) if src[0] == "spare"]
    kept = {src[1] for src, _ in got.values() if src[0] == "base"}
    gone = [k for k in range(len(bullets)) if k not in kept]
    out = _cross_job(sr, company, j, page, got)
    where = lambda pairs: ", ".join(f"bullet {t + 1} of role {j + 1} (spare {s.id})" for t, s in pairs)  # noqa: E731
    if len(ins) > sr.max_swaps:
        out.append(Failure(5, "spare_swaps", f"{_short(company)} takes {len(ins)} spare bullets; at most "
                                             f"{sr.max_swaps} per job: put a base bullet back in place of "
                                             + where(ins[sr.max_swaps:])))
    for anchor in _anchors_of(sr, company):
        k = phrase_bullet(_strip_employer(anchor, company), bullets)
        if k is not None and k in gone and ins:
            out.append(Failure(5, "spare_swaps", f"the anchor bullet '{anchor}' is off the page; anchors are never "
                                                 f"replaced: {where(ins)} must take the place of another bullet, or "
                                                 f"become the anchor again: \"{bullets[k]}\""))
    pool, swaps, placed = list(gone), [], {}
    for t, spare in ins:
        out_k = _pair(spare, bullets, pool, declared.get(spare.id))
        if spare.targets and out_k is None:
            out.append(Failure(5, "spare_swaps", f"bullet {t + 1} of role {j + 1} is spare {spare.id}, which may "
                                                 f"replace only the {' or '.join(spare.targets)} bullet, and that "
                                                 f"bullet is still on the page"))
        if out_k is None and pool and not spare.targets:
            out_k = pool[0]
        if out_k is not None and out_k in pool:
            pool.remove(out_k)
        placed[(j, t)] = spare
        swaps.append({"job": _short(company), "spare_id": spare.id, "tags": spare.tags, "in_at": f"{j + 1}.{t + 1}",
                      "out_bullet": f"{j + 1}.{out_k + 1}" if out_k is not None else None,
                      "out_text": bullets[out_k] if out_k is not None else None,
                      "reason": str((declared.get(spare.id) or {}).get("reason") or "").strip()})
    return out, swaps, placed


def _pair(spare, bullets: list, pool: list, declared: dict | None) -> int | None:
    """The base bullet a spare took the place of: its declared out_bullet when that one is off the page (and allowed
    for a variant), else a variant's target when off the page."""
    m = re.match(r"^\s*\d+\.(\d+)\s*$", str((declared or {}).get("out_bullet") or ""))
    allowed = _targets(spare, bullets) if spare.targets else None
    if m and int(m.group(1)) - 1 in pool and (allowed is None or int(m.group(1)) - 1 in allowed):
        return int(m.group(1)) - 1
    return next((k for k in allowed if k in pool), None) if allowed else None


def _targets(spare, bullets: list) -> list:
    return [k for k in (phrase_bullet(t, bullets) for t in spare.targets) if k is not None]


def _cross_job(sr, company: str, j: int, page: list, got: dict) -> list:
    out = []
    others = [s for s in sr.spares if not skillrules.same_employer(s.job, company)]
    for t, text in enumerate(page):
        mine = got.get(t, (None, 0.0))[1]
        hit = next((s for s in others if similarity(s.text, text) >= max(CROSS_MIN, mine + 1e-9)), None)
        if hit:
            out.append(Failure(5, "spare_swaps", f"bullet {t + 1} of role {j + 1} is spare {hit.id} of {hit.job}; a "
                                                 f"spare goes only into its own job"))
    return out


def _repeated_numbers(tstruct, placed: dict) -> list:
    out = []
    bullets = [(j, t, b) for j, job in enumerate(tstruct.jobs) for t, b in enumerate(job["bullets"])]
    for (j, t), spare in placed.items():
        mine = number_keys(tstruct.jobs[j]["bullets"][t])
        for j2, t2, other in bullets:
            same = sorted(mine & number_keys(other)) if (j2, t2) != (j, t) else []
            if same:
                out.append(Failure(5, "spare_swaps", f"bullet {t + 1} of role {j + 1} (spare {spare.id}) states "
                                                     f"'{same[0]}', which bullet {t2 + 1} of role {j2 + 1} states "
                                                     f"too; no two bullets state the same number"))
                break
    return out
