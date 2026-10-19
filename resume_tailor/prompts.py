"""The prompt for one tailoring attempt: the person's facts.md (fact bank and writing notes, verbatim) + the app's
contract (structured JSON, locked counts, measured line budgets, honesty lists, the JD with its ATS keywords, the
exact failures of the previous attempt). The fact bank is never rewritten; the contract only tells Claude how the
answer has to be shaped so Python can fill the base .docx."""
import json

from . import rules

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


def _ats_block(scores: dict | None, known: set, mirror: bool = False) -> str:
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
        f"the same order, the same {len(structure.skills)} skill lines (you may rename and reorder categories, but "
        f"the count stays {len(structure.skills)}), the same roles in the same order with the same bullet COUNTS "
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
        "- Every number must already appear in the base resumes or the fact bank, unchanged. No new metrics.",
        "- Employers, titles, dates, degree, location: unchanged.",
        ("- Skills and tools: only those in the base resumes or the KNOWN SKILLS list below."
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
        f"- No banned words: spearheaded, leveraged, cutting-edge, seamless, robust, delve, utilize, synergy. No em "
        f"dashes (—) anywhere in bullets; use en dashes (–) only for ranges. {period}",
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
        _ats_block(scores, known, bool(prules.get("jd_skills_allowed"))),
        "",
    ]
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
        "skills, or empty]}.",
    ]
    return "\n".join(parts)


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
    why = ("the plain title for this posting's family (AI Engineer, Machine Learning Engineer, Data Scientist, Data "
           "Engineer or AI/ML Engineer); Python computed it" if prules.get("headline_from_job")
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
           "- Every number must already be in the base resume or the fact bank. No new metrics, tools, "
           "employers or dates. Skills and tools: only the base resumes and KNOWN SKILLS"
           + (", plus every hard skill the JD names (mirror the JD's hard skills)." if prules.get("jd_skills_allowed")
              else "."),
           f"- Era rule: bullets of {', '.join(prules.get('era_jobs') or ()) or 'the earliest job'} never mention "
           f"LLMs, GenAI, agents, RAG, LangChain, MCP, GPT-4, Claude or vector databases.",
           f"- No banned words (spearheaded, leveraged, cutting-edge, seamless, robust, delve, utilize, synergy). "
           f"No em dashes in bullets. {period} Use the JD's exact wording for keywords.",
           f"- The summary stays within {len(structure.summary) + 15} characters"
           + (f" ({sw[0]} to {sw[1]} words, " + (f"exactly {ss[0]}" if ss[0] == ss[1] else f"{ss[0]} to {ss[1]}")
              + " sentences)" if sw and ss else "") + "."]
    out += [f"- {job} bullets open with these verbs, each used once: {', '.join(v)}."
            for job, v in (prules.get("verbs") or {}).items()]
    return out + _summary_lines(prules, base_id)


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
