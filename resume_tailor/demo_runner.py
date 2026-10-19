"""Demo mode: a stand-in for Claude that needs no key and no network, so the whole app (queue, PDF, checks,
review, approve) can be tried and tested offline.

It does what a careful, unimaginative editor would: keep every bullet as it is, and add to the Skills section the
job's keywords the person already owns (from known_skills.txt) while each line still fits on the lines it uses,
dropping a skill the job doesn't mention when that makes room. Repair and note rounds change nothing and say why."""
import json
import re

from . import ats
from .claude_runner import RunResult

DEMO_MODEL = "demo (no AI)"
NOTE = "Demo mode doesn't call Claude, so it can't rewrite text. Add an API key on the Settings page for real tailoring."
_BASE = re.compile(r"=== BASE RESUME \(JSON; refill this\) ===\n(.*?)\n\n=== ", re.S)
_OWNED = re.compile(r"^Of these, the person owns[^:]*: (.*)$", re.M)
_BUDGET = re.compile(r'^- "(?P<cat>[^"]+)": base is \d+ characters of items and renders in (?P<lines>\d+) line', re.M)
_CPL = re.compile(r"A full bullet line holds about (\d+) characters")
_JD = re.compile(r"^(?:Required|Preferred) JD keywords \(\d+\): (.*)$", re.M)
FILL = 0.92                   # share of a line the demo lets a skills line use (category included)


def _result(output: dict) -> RunResult:
    return RunResult(True, output=output, raw={"demo": True}, usage={}, cost_usd=0.0, duration_ms=5,
                     model=DEMO_MODEL, num_turns=1)


# which words in a skills line's own category name suit a lexicon category ("MLOps & Cloud" suits Cloud tools)
LINE_WORDS = {"Languages": ("language", "programming"), "ML & Data Science": ("machine", "ml", "science", "learning"),
              "Deep Learning": ("machine", "ml", "deep", "learning"), "GenAI & Agentic AI": ("genai", "llm", "ai"),
              "LLM & Fine-tuning": ("genai", "llm", "ai"), "Vector Search": ("genai", "llm", "ai", "search"),
              "Data Engineering": ("data", "pipeline"), "Databases": ("data", "database"), "BI & Analytics": ("data", "bi"),
              "Cloud": ("cloud",), "MLOps & DevOps": ("mlops", "devops", "ops", "machine", "ml"), "Backend & APIs": ("api", "backend", "cloud")}


def _fit_score(term: str, line: dict) -> tuple:
    """How well a skills line suits the term: lines named for its kind first, then lines holding its kind."""
    want = ats.category_of(term)
    name = line["category"].lower()
    named = sum(1 for w in LINE_WORDS.get(want or "", ()) if re.search(rf"\b{w}", name))
    holds = sum(1 for item in line["items"] if ats.category_of(item) == want)
    return (named, holds)


def _home_line(term: str, skills: list) -> int:
    return max(range(len(skills)), key=lambda i: (_fit_score(term, skills[i]), -i))


def _terms(text: str | None) -> list:
    return [t.strip() for t in (text or "").split(",") if t.strip() and t.strip() != "none"]


def tailor(prompt: str) -> dict:
    match = _BASE.search(prompt)
    if not match:
        raise ValueError("demo runner: no base resume in the prompt")
    base = json.loads(match.group(1))
    owned = _terms(_OWNED.search(prompt).group(1) if _OWNED.search(prompt) else "")
    wanted = {t for m in _JD.finditer(prompt) for t in _terms(m.group(1))}
    cpl = int(_CPL.search(prompt).group(1)) if _CPL.search(prompt) else 100
    lines = {m.group("cat"): int(m.group("lines")) for m in _BUDGET.finditer(prompt)}
    skills = [{"category": s["category"], "items": list(s["items"])} for s in base["skills"]]

    def fits(line: dict, items: list) -> bool:
        room = int(cpl * lines.get(line["category"], 1) * FILL)
        return len(line["category"]) + 2 + len(", ".join(items)) <= room

    have = ats.find(json.dumps(base, ensure_ascii=False))
    added, dropped = [], []
    for term in owned:
        if term in have:
            continue
        order = sorted(range(len(skills)), key=lambda i: (_fit_score(term, skills[i]), -i), reverse=True)
        suited = [i for i in order if _fit_score(term, skills[i]) > (0, 0)] or order
        target = next((i for i in suited if fits(skills[i], skills[i]["items"] + [term])), None)
        if target is None:                                # make room: drop skills the job never mentions
            for i in suited:
                items = list(skills[i]["items"])
                spare = [x for x in reversed(items) if not (set(ats.find(x)) & wanted)]
                gone = []
                while spare and not fits(skills[i], items + [term]):
                    gone.append(spare.pop(0))
                    items.remove(gone[-1])
                if fits(skills[i], items + [term]):
                    skills[i]["items"] = items + [term]
                    added.append(term)
                    dropped += gone
                    break
            continue
        skills[target]["items"].append(term)
        added.append(term)
    notes = [f"Added {t} to Skills (you list it in known_skills.txt and the job asks for it)" for t in added]
    notes += [f"Dropped {t} from Skills to make room (the job doesn't mention it)" for t in dropped]
    return {"headline": base["headline"], "summary": base["summary"], "skills": skills,
            "jobs": [{"company": j["company"], "bullets": list(j["bullets"])} for j in base["jobs"]],
            "change_notes": notes or ["No change: the base already covers the job's keywords you own"],
            "match": "Demo mode: the base resume with your matching skills added. No AI was used.",
            "review": [NOTE], "gaps": [], "added_from_jd": []}


def run(prompt: str, schema: dict, **_ignored) -> RunResult:
    if prompt.startswith("You are repairing"):
        return _result({"bullets": [], "skills": [], "summary": None, "notes": [NOTE]})
    if prompt.startswith("You are editing"):
        return _result({"bullets": [], "skills": [], "summary": None, "notes": [], "location": None, "cannot": NOTE})
    try:
        return _result(tailor(prompt))
    except (ValueError, KeyError, json.JSONDecodeError) as err:
        return RunResult(False, error=str(err), kind="error", model=DEMO_MODEL)
