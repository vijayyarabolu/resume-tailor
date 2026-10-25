"""The prompt for one tailoring attempt: the person's facts.md (fact bank and writing notes, verbatim) + the app's
contract (structured JSON, locked counts, measured line budgets, honesty lists, the JD with its ATS keywords, the
exact failures of the previous attempt). The fact bank is never rewritten; the contract only tells Claude how the
answer has to be shaped so Python can fill the base .docx."""
import json

from . import rules, skillrules

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "summary", "skills", "jobs", "change_notes", "match", "review", "gaps",
                 "added_from_jd"],
    "properties": {
        "headline": {"type": "string", "description": "title under the name"},
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["category", "items"],
                                               "properties": {"category": {"type": "string"},
                                                              "items": {"type": "array",
                                                                        "items": {"type": "string"}}}}},
        "jobs": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["company", "bullets"],
                                             "properties": {"company": {"type": "string"},
                                                            "bullets": {"type": "array",
                                                                        "items": {"type": "string"}}}}},
        "change_notes": {"type": "array", "items": {"type": "string"}},
        "match": {"type": "string"},
        "review": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "added_from_jd": {"type": "array", "items": {"type": "string"}},
    },
}
HM_SCHEMA = {                                     # rules.toml hm_review: the hiring-manager pass (report only)
    "type": "object",
    "additionalProperties": False,
    "required": ["reads_human", "clear_fit", "score", "notes"],
    "properties": {
        "reads_human": {"type": "boolean", "description": "reads like a person wrote it"},
        "clear_fit": {"type": "boolean", "description": "the fit for THIS job is clear in a 10-second scan"},
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}
MIRROR_PROPS = {                                  # rules.toml mirror_duties: the JD's responsibilities on real bullets
    "jd_responsibilities": {"type": "array", "items": {"type": "string"},
                            "description": "the JD's 3-5 core responsibilities, in the JD's own words"},
    "jd_domain": {"type": "array", "items": {"type": "string"},
                  "description": "up to 6 business or industry domain nouns of the JD (never skills)"},
    "mirrored": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                            "required": ["responsibility", "bullet_id", "phrase_used"],
                                            "properties": {"responsibility": {"type": "string"},
                                                           "bullet_id": {"type": "string"},
                                                           "phrase_used": {"type": "string"}}}},
    "unmatched": {"type": "array", "items": {"type": "string"}},
}
MIRROR_SCHEMA = {**SCHEMA, "required": SCHEMA["required"] + list(MIRROR_PROPS),
                 "properties": {**SCHEMA["properties"], **MIRROR_PROPS}}
_ROW_ITEMS = {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["row", "items"],
                                         "properties": {"row": {"type": "string"},
                                                        "items": {"type": "array", "items": {"type": "string"}}}}}
TAILOR_PROPS = {                                  # rules.toml spare_swaps / skills_add_first: what changed and where
    "swaps": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "required": ["job", "out_bullet", "spare_id", "reason"],
                                         "properties": {"job": {"type": "string"},
                                                        "out_bullet": {"type": "string",
                                                                       "description": "the base bullet id, e.g. 1.5"},
                                                        "spare_id": {"type": "string"},
                                                        "reason": {"type": "string"}}}},
    "skills_added": _ROW_ITEMS,
    "skills_removed": _ROW_ITEMS,
}
MAX_JD_CHARS = 14000
REPAIR_JD_CHARS = 6000

REPAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["bullets", "skills", "summary", "notes"],
    "properties": {
        "bullets": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                "required": ["job", "index", "text"],
                                                "properties": {"job": {"type": "integer"},
                                                               "index": {"type": "integer"},
                                                               "text": {"type": "string"}}}},
        "skills": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["index", "category", "items"],
                                               "properties": {"index": {"type": "integer"},
                                                              "category": {"type": "string"},
                                                              "items": {"type": "array",
                                                                        "items": {"type": "string"}}}}},
        "summary": {"type": ["string", "null"]},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}
NOTE_SCHEMA = {                                   # the note round may also move the contact location
    **REPAIR_SCHEMA,
    "required": REPAIR_SCHEMA["required"] + ["location", "cannot"],
    "properties": {**REPAIR_SCHEMA["properties"],
                   "location": {"type": ["string", "null"], "description": "City, ST for the contact line"},
                   "cannot": {"type": ["string", "null"], "description": "what the note asked that was not done"}},
}


def _skill_budget(base_structure, measure) -> list:
    lines = []
    for s in base_structure.skills:
        n = measure.skill_lines.get(s.category, 1)
        lines.append(f'- "{s.category}": base is {len(s.items)} characters of items and renders in {n} line(s). '
                     f"Keep this line within {len(s.items) + (8 if n > 1 else 4)} characters so it still fits in "
                     f"{n} line(s).")
    return lines


def _owned_found(scores: dict) -> set:
    """JD terms the keyword model found in the person's own texts: the base resumes, known skills, the fact bank."""
    found = scores.get("found") or {}
    return {t for key, d in found.items() if key not in ("tailored", "jd")
            and not key.startswith(("era_job_", "skills_row_")) for t in d}


def _ats_block(scores: dict | None, known: set, mirror: bool = False, fixed_rows: bool = False,
               add_first: bool = False) -> str:
    if not scores:
        return "(ATS keywords unavailable)"
    terms = scores.get("terms") or []
    by_id = {s["id"]: s for s in scores.get("scores") or []}
    need = [t["name"] for t in terms if t.get("need")]
    nice = [t["name"] for t in terms if not t.get("need")]
    out = [f"ATS model: {scores.get('label')}",
           f"Required JD keywords ({len(need)}): " + ", ".join(need),
           f"Preferred JD keywords ({len(nice)}): " + ", ".join(nice)]
    known_wanted = [n for n in need + nice if n in known]
    if add_first:       # rules.toml skills_add_first: trim only by the row's [drop_order]
        owned = set(known_wanted) | _owned_found(scores)
        wanted = [n for n in need + nice if n in owned]
        out.append("Of these, the person owns (in the base resumes, known skills or the fact bank; SKILLS: ADD FIRST: "
                   "every one not on the page yet goes into the row where it belongs, JD skills first in the row; a "
                   "bullet names one only where the fact bank supports that work): " + (", ".join(wanted) or "none"))
        gaps = [n for n in need + nice if n not in owned]
        if gaps:
            out.append("Every other JD keyword is a Gap the person does not have: never add it to Skills or a bullet, "
                       "not even as a near synonym: " + ", ".join(gaps))
    elif fixed_rows:    # allowed, never forced; a bullet only names work the fact bank supports
        out.append("Of these, the person owns (known skills; you MAY add one to the fitting Skills row when the row "
                   "keeps its line count, and to a bullet only where the fact bank supports that work): "
                   + (", ".join(known_wanted) or "none"))
    else:
        out.append("Of these, the person owns (known skills; add every one of them to Skills and to one bullet): "
                   + (", ".join(known_wanted) or "none"))
    if mirror:
        out.append("Mirror the JD: add EVERY required and preferred keyword above to Skills and to one fitting bullet "
                   "(required ones are checked by machine).")
    for s in by_id.values():
        out.append(f"Base '{s.get('label')}': {s.get('pct')}% keyword match; missing: "
                   + (", ".join(s.get("missing") or [])[:600] or "nothing"))
    return "\n".join(out)


def build(profile, base, measure, jd_text: str, company: str, role: str, scores: dict | None,
          failures: list | None = None, note: str | None = None, attempt: int = 1, base_why: str = "") -> str:
    prules = job_rules(profile, base, role)
    structure = base.structure
    content = structure.as_content()
    known = set(profile.known_skills)
    cpl = measure.chars_per_line or 110
    min_chars = int(cpl * 1.3)
    max_chars = int(cpl * 2 - 6)
    counts = ", ".join(f"{j['company']}: {len(j['bullets'])}" for j in structure.jobs)
    hard = prules.get("bullet_words") or (15, 25)
    bw = prules.get("bullet_words_target") or hard
    sw = prules.get("summary_words")
    ss = prules.get("summary_sentences")
    period = ("End every bullet with a period." if measure.bullets_end_with_period
              else "No period at the end of a bullet.")
    parts = [
        f"You are tailoring {profile.name}'s resume for one job. Follow the FACT BANK below for every writing decision "
        f"(voice, base selection logic, facts, industry words, keyword rules). This is batch mode: never ask "
        f"questions, never wait for a go; produce the finished resume as ONE JSON object matching the schema. "
        f"No text outside the JSON.",
        "",
        "=== FACT BANK (the person's facts and writing notes; verbatim) ===",
        profile.skill_text.strip() or "(no facts.md yet: use only the base resume)",
        "",
        "=== CONTRACT (how the answer must be shaped; overrides any output format the fact bank describes) ===",
        f"The base resume below is the exact .docx that Python will refill. Its layout is locked: same sections in "
        f"the same order, the same {len(structure.skills)} skill lines ("
        + ("their names and order are fixed; reorder the items inside a row so the JD's terms come first"
           if prules.get("skill_rows_fixed") else
           f"you may rename and reorder categories, but the count stays {len(structure.skills)}")
        + f"), the same roles in the same order with the same bullet COUNTS "
        f"({counts}), same company names, titles and dates character for character. Only the "
        + ("headline, the " if prules.get("headline_may_change", True) else "")
        + "summary, the skill lines and the bullet texts change.",
        *_title_lines(prules, base),
        f"Base chosen: '{base.label}' ({base_why or 'best ATS score for this JD'}).",
        "",
        "Measured line budget of this base (the PDF must stay exactly 1 page, US Letter):",
        f"- A full bullet line holds about {cpl:.0f} characters. Every bullet must fill exactly 2 rendered lines: "
        f"between {min_chars} and {max_chars} characters. A bullet whose second line is shorter than a fifth of "
        f"the width is a widow and fails; a bullet that spills to a third line fails.",
        f"- HARD LIMIT, counted by machine: {hard[0]} to {hard[1]} words per bullet (aim for {bw[0]} to {bw[1]}). Count "
        f"the words of every bullet before answering; a {hard[1] + 1}-word bullet fails the whole attempt.",
        f"- The summary is {len(structure.summary)} characters in the base; keep yours within "
        f"{len(structure.summary) + 15} characters"
        + (f" ({sw[0]} to {sw[1]} words, " + (f"exactly {ss[0]}" if ss[0] == ss[1] else f"{ss[0]} to {ss[1]}")
           + " sentences)" if sw and ss else "") + ".",
        "- Skill lines (bold category + colon, then comma-separated items; the category text itself counts):",
        *_skill_budget(structure, measure),
        "",
        "Honesty (checked by machine, every failure costs a retry):",
        _numbers_line(prules),
        "- Employers, titles, dates, degree, location: unchanged.",
        ("- Skills and tools: only those in the base resumes or the KNOWN SKILLS list below."
         + (" A JD skill in neither is a GAP: never add it, never swap in a near-equivalent (AWS work is not Azure); "
            "Python lists the gaps for the person." if prules.get("jd_gaps") else "")
         if not prules["jd_skills_allowed"] else
         "- MIRROR THE JD'S HARD SKILLS: every hard skill the JD names, required AND preferred, programming "
         "languages included, goes into Skills AND is woven into one fitting bullet (the person knows them and "
         "answers for them in the interview). List each one that is not in the base resumes or KNOWN SKILLS in "
         "added_from_jd. Era rules still apply (no GenAI/LLM/agent/vector tools in the earliest job). Soft skills, "
         "degree names and vague words are never added; no invented numbers, projects, employers, titles or years."),
        f"- Era rule: bullets of {', '.join(prules['era_jobs']) or 'the earliest job'} never mention LLMs, GenAI, "
        f"agents, RAG, LangChain, LangGraph, MCP, AgentCore, Semantic Kernel, GPT-4, Claude or vector databases.",
        "",
        "Writing (checked by machine):",
        f"- No banned words: spearheaded, leveraged, cutting-edge, seamless, robust, delve, utilize, synergy. "
        + ("No semicolons, em dashes, en dashes or parentheses inside bullets (hyphen ranges like 4-6 are fine). "
           if prules.get("bullet_plain") else "No em dashes (—) anywhere in bullets; use en dashes (–) only for "
                                              "ranges. ") + period,
        *_shape_lines(prules),
        *_summary_lines(prules, base.id),
        *[f"- {job} bullets open with these verbs, each used once, any order: {', '.join(v)}."
          for job, v in (prules.get("verbs") or {}).items()],
        "- Use the JD's exact wording for keywords (no synonyms, no plural changes).",
        "",
        "=== BASE RESUME (JSON; refill this) ===",
        json.dumps(content, ensure_ascii=False, indent=1),
        "",
        "=== KNOWN SKILLS (the person owns these) ===",
        ", ".join(profile.known_skills),
        "",
        f"=== THE JOB: {company}, {role} ===",
        jd_text[:MAX_JD_CHARS],
        "",
        "=== ATS KEYWORDS (the keyword model's reading of this JD) ===",
        _ats_block(scores, known, bool(prules.get("jd_skills_allowed")), bool(prules.get("skill_rows_fixed")),
                   bool(prules.get("skills_add_first"))),
        "",
    ]
    if prules.get("mirror_duties"):
        parts += _mirror_lines(structure, prules)
    if prules.get("spare_swaps") or prules.get("skills_add_first"):
        parts += _tailor_lines(structure, prules, skillrules.for_rules(prules, profile.fact_bank))
    if note:
        parts += ["=== INSTRUCTION FROM THE USER FOR THIS RUN ===", note, ""]
    if failures:
        parts += [f"=== ATTEMPT {attempt}: THE PREVIOUS ATTEMPT FAILED THESE CHECKS. FIX EXACTLY THESE, keep "
                  f"everything else as it was ===", *[f"- {f}" for f in failures], ""]
    parts += [
        "=== OUTPUT ===",
        "Return one JSON object: {headline, summary, skills: [{category, items: [..]}], jobs: [{company, bullets: "
        "[..]}] (same company strings and bullet counts as the base, in base order), change_notes: [what you changed "
        "and why, one line each], match: [one line on how well the resume fits the job], review: [the hiring-manager/ATS review bullets], "
        "gaps: [unfixable gaps, or empty], added_from_jd: [JD skills you added that are not in the base or known "
        "skills, or empty]"
        + ("; jd_responsibilities, jd_domain, mirrored: [{responsibility, bullet_id, phrase_used}], unmatched "
           "(see JD RESPONSIBILITIES)" if prules.get("mirror_duties") else "")
        + ("; swaps: [{job, out_bullet, spare_id, reason}], skills_added: [{row, items}], skills_removed: [{row, "
           "items}] (see SPARE BULLETS AND SKILLS BY ROW)}."
           if prules.get("spare_swaps") or prules.get("skills_add_first") else "}."),
    ]
    return "\n".join(parts)


def schema_for(prules: dict) -> dict:
    """The full round's answer schema: with rules.toml `mirror_duties` it carries the mirrored responsibilities, with
    `spare_swaps` or `skills_add_first` the swaps and the skills added and removed by row."""
    out = MIRROR_SCHEMA if prules.get("mirror_duties") else SCHEMA
    if prules.get("spare_swaps") or prules.get("skills_add_first"):
        out = {**out, "required": out["required"] + list(TAILOR_PROPS),
               "properties": {**out["properties"], **TAILOR_PROPS}}
    return out


def _mirror_lines(structure, prules: dict) -> list:
    """Recruiters scan for the job's own responsibility language. Same call, no extra Claude call."""
    ids = [f"- {j}.{b} ({job['company'].split(',')[0]}): {text[:90]}"
           for j, job in enumerate(structure.jobs, 1) for b, text in enumerate(job["bullets"], 1)]
    domains = "; ".join(f"{k}: {', '.join(v[:4])}" for k, v in (prules.get("employer_domains") or {}).items())
    return [
        "=== JD RESPONSIBILITIES (mirror them; same answer, checked by machine) ===",
        "1. Extract: list the JD's 3 to 5 core responsibilities in jd_responsibilities (the JD's own words, short: "
        "\"partner with product and data science\", \"own models end to end\", \"build evaluation pipelines\") and "
        "up to 6 domain nouns in jd_domain: the business or industry the job serves (payments, healthcare claims, ad "
        "ranking, logistics ...), never a skill or technique (evaluation, observability, machine learning are not "
        "domains).",
        "2. Map: for each responsibility pick the ONE base bullet below whose underlying work truly matches it (use "
        "the fact bank). Reword that bullet so it uses the JD's verb and phrasing, and record {responsibility, "
        "bullet_id (the base id below, e.g. \"2.1\"), phrase_used (the exact words now on the bullet, copied "
        "character for character)} in mirrored. A bullet carries at most one mirrored phrase. When no bullet's real "
        "work matches, put the responsibility in unmatched and leave it out of the resume.",
        "3. Rules: no new claims, projects, tools or numbers ("
        + ("numbers only from the base resume and " + _fact_numbers(prules) if prules.get("fact_bank_numbers") else
           "numbers only from the base resume and the fact bank")
        + "). A domain noun goes on a bullet only where that job's real work touched the domain ("
        + (domains or "per the fact bank") + "); a freight company's work fits \"logistics\", not \"healthcare\". "
        "Never soft-skill lists or vague words (team player, passionate, dynamic). Every rule above still holds: "
        "the era rule, no banned words, word counts"
        + (", opening verbs unique" if prules.get("verbs_unique") else "")
        + (f", at most {prules['max_from_to']} \"from X to Y\"" if prules.get("max_from_to") is not None else "")
        + ".",
        "Base bullet ids:",
        *ids,
        "",
    ]


def _tailor_lines(structure, prules: dict, sr) -> list:
    """The spare bullets by job (ids from facts.md Part 3), the anchors, and how swaps and skill changes come back.
    Python reads the swaps from the page and checks them (tailorchecks)."""
    out = ["=== SPARE BULLETS AND SKILLS BY ROW (same answer, checked by machine) ==="]
    if prules.get("spare_swaps"):
        out.append(f"A spare may replace one bullet in its OWN job when it covers the JD clearly better (at most "
                   f"{sr.max_swaps} per job; never an anchor: {', '.join(sr.anchors) or 'none'}; a \"variant of X\" "
                   f"spare replaces only X). It keeps its facts and numbers and may take JD wording. Base bullet ids "
                   f"are job.bullet, as 1.2 for the second bullet of the first job.")
        for j, job in enumerate(structure.jobs, 1):
            mine = sr.spares_of(job["company"])
            if mine:
                out.append(f"Spares for {job['company'].split(',')[0]} (job {j}):")
                out += [f"- {s.id} ({s.tags}): {s.text}" for s in mine]
        out.append("Return swaps: [{job (company), out_bullet (the base id it replaced, e.g. \"1.5\"), spare_id, "
                   "reason (a few words: what the JD stresses)}] for every spare used; [] when none.")
    if prules.get("skills_add_first"):
        out.append("Return skills_added and skills_removed: [{row (the row name), items}] for each row that gained or "
                   "lost a skill; [] when none.")
    return out + [""]


def _fact_numbers(prules: dict, bank: str = "the fact bank") -> str:
    """Where a number may come from with fact_bank_numbers: Part 1 of facts.md, and Part 3 with spare bullets."""
    return f"{bank} above its Part 2" + (" or in a spare bullet (its Part 3)" if prules.get("spare_swaps") else "")


def _numbers_line(prules: dict) -> str:
    if prules.get("fact_bank_numbers"):
        return (f"- Every number must already appear in this base resume or {_fact_numbers(prules, 'the FACT BANK')}, unchanged. "
                "The fact bank's Part 2 (interview details) may shape wording but adds NO number. No new metrics, no "
                "rounding up.")
    return "- Every number must already appear in the base resumes or the fact bank, unchanged. No new metrics."


def _shape_lines(prules: dict) -> list:
    """The stricter rules.toml keys: what may change and how bullets are shaped, as the checks read it."""
    out = []
    if prules.get("summary_keep"):
        lo, hi = prules["summary_keep"]
        out.append(f"- Summary: only sentence 1 may adapt its focus words to the JD; sentences {lo}-{hi} stay the "
                   f"base's word for word.")
    if prules.get("summary_structure"):
        spec = prules["summary_structure"]
        opening = " ".join(x for x in (prules.get("summary_start") or "<title> with <years>", spec.get("verb")) if x)
        industries = ", ".join(prules.get("summary_industries") or ())
        out.append("- Summary: written fresh for THIS JD (never the base's sentences): sentence 1 opens "
                   f"\"{opening}\" + the 2 or 3 kinds of work this JD cares about most"
                   + (f" + \"across\" + industries from: {industries}" if industries else "")
                   + ", then 2 proofs"
                   + (f"; at most {spec['max_numbers']} numbers besides the years" if spec.get("max_numbers") is not None
                      else "")
                   + (f"; at least {spec['min_employers']} employer(s) named" if spec.get("min_employers") else "")
                   + "; sentences 2 and 3 open differently and never repeat a bullet.")
    if prules.get("skill_rows_fixed"):
        out += ["- Bullets: you may reorder bullets WITHIN a job (strongest match first) and reword them with the JD's "
                "exact terms only for work the base, fact bank or spare bullets support. Never a new tool, metric or "
                "claim.",
                "- Skills rows keep their names, order and rendered line counts; no row may end with one dangling "
                "word on its last line. "
                + ("SKILLS: ADD FIRST: put every JD skill the person has into its row, JD skills first, the rest in "
                   "base order; only when the row overflows, remove the first skill of that row's DROP ORDER ("
                   + ("; ".join(f"{k}: {', '.join(v)}" for k, v in (prules.get("drop_order") or {}).items())
                      or "none given, so remove nothing")
                   + ") the JD does not mention, one at a time; never a LOCKED skill ("
                   + (", ".join(prules.get("locked_skills") or ()) or "none") + ") or a skill the JD names."
                   if prules.get("skills_add_first") else
                   "Reorder the items inside a row; never drop or rename a base skill item (an allowed JD skill "
                   "joins a row only when the row still keeps its line count).")]
    if prules.get("verbs_unique"):
        out.append("- Every bullet's opening verb differs from every other bullet's, across the whole resume.")
    if prules.get("short_bullets"):
        limit, need = prules["short_bullets"]
        out.append(f"- Vary bullet lengths: at least {need} bullets have {limit} words or fewer"
                   + (f"; at most {prules['max_at_top']} have exactly {prules['bullet_words'][1]} words."
                      if prules.get("max_at_top") and prules.get("bullet_words") else "."))
    if prules.get("max_from_to") is not None:
        out.append(f"- At most {prules['max_from_to']} \"from X to Y\" phrasings in all bullets together; keep "
                   f"the other number shapes (92%, halved, up from 40%, 1-2 a month from 4-5).")
    return out


def job_rules(profile, base, role: str) -> dict:
    """The person's rules for this job, with its headline and summary opening."""
    prules = rules.for_profile(profile.id, profile.rules)
    return rules.for_job(prules, role, base.structure.headline)


def _title_lines(prules: dict, base) -> list:
    """the title under the name is one of five plain titles picked by Python from the
    posting title's family, or the base's own title when the family is unclear; or one fixed title for every job
    (rules.toml `headline`)."""
    if prules.get("headline_may_change", True):
        return []
    title = rules.headline_of(prules, base.structure.headline)
    names = ", ".join(prules.get("headline_titles") or ("AI Engineer", "Machine Learning Engineer", "Data Scientist",
                                                          "Data Engineer", "AI/ML Engineer"))
    why = (f"the plain title for this posting's family (one of {names}); Python computed it"
           if prules.get("headline_from_job")
           else "this person's title for every job, whatever the posting calls the role")
    return [f"TITLE RULE (overrides the fact bank): the title under the name is exactly \"{title}\" ({why}). Never add a "
            f"word, never a level word, never a made-up title (such as \"Applied AI Research Scientist\"). Return "
            f"\"headline\": \"{title}\". The summary opens with this title once, never with a second title."]


def _summary_lines(prules: dict, base_id: str) -> list:
    start = rules.summary_start(prules, base_id)
    out = [f"- The summary must open with \"{start}\" (the same title as under the name)."] if start else []
    return out + [f"- The summary must contain \"{m}\"." for m in prules.get("summary_must_contain") or ()]


def _rule_lines(prules: dict, measure, structure, base_id: str = "") -> list:
    cpl = measure.chars_per_line or 110
    hard = prules.get("bullet_words") or (15, 25)
    sw, ss = prules.get("summary_words"), prules.get("summary_sentences")
    period = ("End every bullet with a period." if measure.bullets_end_with_period
              else "No period at the end of a bullet.")
    out = [f"- A bullet fills exactly 2 rendered lines: {int(cpl * 1.3)} to {int(cpl * 2 - 6)} characters "
           f"(about {cpl:.0f} per line), {hard[0]} to {hard[1]} words counted by machine. A last line under a fifth "
           f"of the width is a widow and fails; a third line fails.",
           (f"- Every number must already be in this base resume or {_fact_numbers(prules)}. No new metrics, tools, "
            if prules.get("fact_bank_numbers") else
            "- Every number must already be in the base resume or the fact bank. No new metrics, tools, ")
           + "employers or dates. Skills and tools: only the base resumes and KNOWN SKILLS"
           + (", plus every hard skill the JD names (mirror the JD's hard skills)." if prules.get("jd_skills_allowed")
              else "."),
           f"- Era rule: bullets of {', '.join(prules.get('era_jobs') or ()) or 'the earliest job'} never mention "
           f"LLMs, GenAI, agents, RAG, LangChain, MCP, GPT-4, Claude or vector databases.",
           f"- No banned words (spearheaded, leveraged, cutting-edge, seamless, robust, delve, utilize, synergy). "
           + ("No semicolons, em/en dashes or parentheses in bullets. " if prules.get("bullet_plain")
              else "No em dashes in bullets. ") + f"{period} Use the JD's exact wording for keywords.",
           f"- The summary stays within {len(structure.summary) + 15} characters"
           + (f" ({sw[0]} to {sw[1]} words, " + (f"exactly {ss[0]}" if ss[0] == ss[1] else f"{ss[0]} to {ss[1]}")
              + " sentences)" if sw and ss else "") + "."]
    out += [f"- {job} bullets open with these verbs, each used once: {', '.join(v)}."
            for job, v in (prules.get("verbs") or {}).items()]
    return out + _shape_lines(prules) + _summary_lines(prules, base_id)


def build_repair(profile, base, measure, content: dict, plan_, jd_text: str, company: str, role: str,
                 scores: dict | None, round_no: int) -> str:
    """Repair mode: only the failing items go back to Claude, with the rule each breaks; everything
    that passed is shown frozen for context and must not be returned."""
    prules = job_rules(profile, base, role)
    structure = base.structure
    item_lines = []
    for n, item in enumerate(plan_.items, 1):
        item_lines += [f"{n}. {item.where}", f"   current: {item.current}"]
        item_lines += [f"   breaks: {r}" for r in item.rules]
    parts = [
        f"You are repairing {profile.name}'s tailored resume for {company}, {role} (repair round {round_no}). "
        f"The resume below already passed most checks. Rewrite ONLY the items listed under ITEMS TO FIX; every "
        f"other line is frozen and must not be returned. Think briefly; answer with ONE JSON object matching the "
        f"schema and nothing else.",
        "",
        "=== FACT BANK (the person's facts and writing notes; verbatim) ===",
        profile.skill_text.strip() or "(no facts.md yet: use only the base resume)",
        "",
        "=== RULES THE MACHINE CHECKS ===",
        *_rule_lines(prules, measure, structure, base.id),
        "",
        "=== THE RESUME AS IT IS NOW (frozen context; same company strings, bullets numbered from 1) ===",
        json.dumps({"headline": content.get("headline"), "summary": content.get("summary"),
                    "skills": content.get("skills"), "jobs": content.get("jobs")}, ensure_ascii=False, indent=1),
        "",
        "=== ITEMS TO FIX ===",
        *(item_lines or ["(none)"]),
        "",
    ]
    if plan_.keywords:
        parts += ["=== ATS: add these JD keywords (the person owns them) ===",
                  "Add each to the skills line where it belongs and, if natural, to at most "
                  f"{2} bullets of your choice (return those bullets too): " + ", ".join(plan_.keywords), ""]
    if plan_.shorten_lines:
        parts += [f"=== LENGTH: the page is {plan_.shorten_lines} rendered line(s) too long ===",
                  "Shorten the listed items so the whole page loses at least that many lines: fewer characters, "
                  "same facts and numbers, still 2 lines per bullet where the rule requires it.", ""]
    parts += [
        "=== KNOWN SKILLS ===", ", ".join(profile.known_skills), "",
        f"=== THE JOB: {company}, {role} (excerpt) ===", jd_text[:REPAIR_JD_CHARS], "",
        "=== OUTPUT ===",
        "Return {bullets: [{job, index, text}] for each bullet you rewrote (job and index are 1-based, as listed), "
        "skills: [{index, category, items: [..]}] for each skills line you rewrote, summary: the new summary or "
        "null, notes: [one line per change]}. Items you did not list are left untouched.",
    ]
    return "\n".join(parts)


def build_note(profile, base, measure, content: dict, note: str, failures: list, jd_text: str, company: str,
               role: str, location: str | None = None) -> str:
    """"Fix with note": the user reviewed the resume and names what to change. ONE call: Claude
    returns only the items the note names; everything else is frozen; nothing runs afterwards that could undo it."""
    prules = job_rules(profile, base, role)
    parts = [
        f"You are editing {profile.name}'s tailored resume for {company}, {role}. The user reviewed it and wrote "
        f"the note below. Change ONLY what the note names. Every other line is frozen: do not return it, do not "
        f"\"improve\" it. Think briefly; answer with ONE JSON object matching the schema and nothing else.",
        "",
        "=== THE USER'S NOTE (do exactly this, nothing more) ===",
        note.strip(),
        "",
        "=== WHAT THE NOTE SAYS IS CONFIRMED ===",
        f"{profile.name} wrote this note. A skill, tool or fact the note says {profile.name} has is true: use it "
        "as the note asks (a skill goes into the fitting Skills line and, where the note asks, into a fitting "
        "bullet). A number the note states may be used. This is the ONE answer to the note: nothing checks or "
        "rewrites it afterwards, so do it fully and keep every machine rule below.",
        f"The contact line's location is now: {location or '(the person own city)'}. If the note asks for a "
        "different location on the top line (beside the e-mail), return it as \"City, ST\" in `location`; "
        "otherwise location is null.",
        "If a part of the note cannot be done under the rules (the title under the name is fixed; the layout keeps "
        "the same number of bullets and skills lines; never invent employers, titles, dates or numbers the note "
        "does not state), do the rest and say in `cannot` what you did not do and why, in one plain sentence; "
        "otherwise cannot is null.",
        "",
        "=== FACT BANK (the person's facts and writing notes; verbatim) ===",
        profile.skill_text.strip() or "(no facts.md yet: use only the base resume)",
        "",
        "=== RULES THE MACHINE CHECKS (an edited item must still pass them) ===",
        *_rule_lines(prules, measure, base.structure, base.id),
        "",
        "=== THE RESUME AS IT IS NOW (same company strings; jobs and bullets numbered from 1 in this order) ===",
        json.dumps({"headline": content.get("headline"), "summary": content.get("summary"),
                    "skills": content.get("skills"), "jobs": content.get("jobs")}, ensure_ascii=False, indent=1),
        "",
        "=== OPEN CHECK FAILURES (for information only; fix one only if the note asks for it) ===",
        *([f"- {f}" for f in failures] or ["(none)"]),
        "",
        "=== KNOWN SKILLS ===", ", ".join(profile.known_skills), "",
        f"=== THE JOB: {company}, {role} (excerpt) ===", jd_text[:REPAIR_JD_CHARS], "",
        "=== OUTPUT ===",
        "Return {bullets: [{job, index, text}] only for bullets the note names (job and index are 1-based), skills: "
        "[{index, category, items: [..]}] only for skills lines the note names (index 1-based), summary: the new "
        "summary if the note names it, else null, notes: [one line per change], location, cannot}. If the note asks to keep "
        "something as it was in the base resume, return the base's text for it.",
        "",
        "=== THE BASE RESUME (for \"as it was\" requests) ===",
        json.dumps(base.structure.as_content(), ensure_ascii=False, indent=1),
    ]
    return "\n".join(parts)


def build_hm(profile, content: dict, jd_text: str, company: str, role: str) -> str:
    """The hiring manager reads the finished resume against the JD. A report for the person: it adds no
    fact, tool or number and changes nothing."""
    resume = {"headline": content.get("headline"), "summary": content.get("summary"),
              "skills": content.get("skills"), "jobs": content.get("jobs")}
    return "\n".join([
        f"You are the hiring manager for {role} at {company}. Read {profile.name}'s resume below against the job "
        f"description, the way you would before deciding whether to call. Report only; you change nothing.",
        "Answer: reads_human (does it read like a person wrote it, no machine-made phrasing?), clear_fit (is the fit "
        "for THIS job clear in a 10-second scan?), score (1-10, would you call), and 3-5 short notes: what you check "
        "first and whether you find it, what reads machine-made, what to move up. Never suggest a fact, tool, number "
        "or claim that is not already on the resume. Plain words, no flattery. ONE JSON object, nothing else.",
        "",
        "=== THE RESUME ===",
        json.dumps(resume, ensure_ascii=False, indent=1),
        "",
        f"=== THE JOB: {company}, {role} ===",

        jd_text[:MAX_JD_CHARS],
    ])
