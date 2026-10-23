"""Per-person writing rules the checks enforce. They live in each profile's rules.toml, a file the person edits
(in a text editor or on the Settings page); this module reads it and fills in defaults. The fact bank
(facts.md) stays the source of truth for how to write; rules.toml only carries what a machine can verify.

A rules.toml looks like this (every key is optional):

    bullet_words = [15, 25]          # hard limit on words per bullet
    bullet_words_target = [20, 25]   # what the prompt asks for
    summary_words = [50, 80]
    summary_sentences = [3, 4]
    min_bullet_lines = 2             # a bullet that renders on one line fails
    era_jobs = ["Northwind"]         # roles from before GenAI tools: no LLM / RAG / vector DB words in them
    headline_from_job = true         # the title under the name follows the posting (headline.py)
    summary_years = "6 years"        # the summary opens "<title> with 6 years"
    summary_must_contain = ["6 years"]
    jd_skills_allowed = false        # true: hard skills from the JD may be added, listed as "Added from JD"
    location = "Portland, OR"        # the contact line's city when the job is remote
    banned_words = ["passionate"]    # added to the built-in list

    [verbs]                          # optional: the opening verbs one role's bullets must use, each once
    "Lumen Freight" = ["Built", "Shipped", "Cut"]

Stricter rules for one master resume (all off by default):

    headline_titles = ["AI Engineer", "Machine Learning Engineer"]   # headline_from_job picks only these; a data
                                     # family or an unknown title maps to the first
    summary_keep = [2, 3]            # summary sentences 2-3 stay the base's word for word
    verbs_unique = true              # every bullet opens with its own verb, across the whole resume
    short_bullets = [27, 4]          # at least 4 bullets have 27 words or fewer (varied lengths)
    max_at_top = 5                   # at most 5 bullets at exactly the top of bullet_words
    bullet_plain = true              # no semicolons, em/en dashes or parentheses inside a bullet
    max_from_to = 4                  # at most 4 "from X to Y" phrasings in all bullets
    fact_bank_numbers = true         # numbers only from this base and facts.md above a "## Part 2" heading (and
                                     # confirmed notes), never from other bases or Part 2
    jd_gaps = true                   # JD skills the person lacks are listed as Gaps (never added)
    skill_rows_fixed = true          # skill row names, order, items and rendered line counts stay the base's
    hm_review = true                 # one report-only hiring-manager pass on the finished resume
    mirror_duties = true             # the JD's 3-5 responsibilities are mirrored on the bullets whose real work
                                     # matches (same Claude call); checks.check_mirror guards each one

    [employer_domains]               # with mirror_duties: a JD domain noun goes on a role's bullet only if listed
    "Lumen Freight" = ["logistics", "freight", "supply chain"]
"""
import re
import tomllib
from pathlib import Path

from . import headline as headline_rule

# AI tells. Resume text never uses these words.
BANNED = (r"\bspearhead\w*", r"\bleverag\w*", r"\bcutting[- ]edge\b", r"\bseamless\w*", r"\brobust\w*",
          r"\bdelv\w*", r"\butiliz\w*", r"\bsynerg\w*", r"\bgame[- ]chang\w*", r"\bstate[- ]of[- ]the[- ]art\b",
          r"\bbest[- ]in[- ]class\b", r"\bworld[- ]class\b", r"\bpassionate\b", r"\bresults[- ]driven\b")
BANNED_RE = re.compile("|".join(BANNED), re.I)
EM_DASH = "—"

# lexicon categories that are GenAI-era (not allowed in an era job)
GENAI_CATEGORIES = ("GenAI & Agentic AI", "LLM & Fine-tuning")
# plus vector search tools whatever category the lexicon files them under
GENAI_TERMS = frozenset({"Pinecone", "OpenSearch", "Weaviate", "Chroma", "ChromaDB", "Milvus", "FAISS", "Qdrant",
                         "Azure AI Search", "Semantic Search", "Vector Databases", "pgvector", "Neo4j"})

DEFAULT_RULES = {"era_jobs": (), "headline_may_change": True, "headline_from_job": False, "fallback_headline": None,
                 "home_location": None, "headline_titles": None, "summary_keep": None, "verbs_unique": False,
                 "short_bullets": None, "max_at_top": None, "bullet_plain": False, "max_from_to": None,
                 "jd_gaps": False, "fact_bank_numbers": False, "skill_rows_fixed": False, "hm_review": False,
                 "mirror_duties": False, "employer_domains": None,
                 "summary_years": None, "headline": None, "summary_must_contain": (), "summary_start": None,
                 "verbs": None, "min_bullet_lines": 2, "jd_skills_allowed": False, "location": None,
                 "bullet_words": None, "bullet_words_target": None, "summary_words": None, "summary_sentences": None,
                 "banned_words": ()}
RANGES = ("bullet_words", "bullet_words_target", "summary_words", "summary_sentences", "summary_keep")
LISTS = ("era_jobs", "summary_must_contain", "banned_words", "headline_titles")
# a bullet's last line under this share of the column is a widow; settings.json `widow_share` overrides it
WIDOW_SHARE = 0.20


class RulesError(ValueError):
    pass


def widow_share() -> float:
    from . import config
    try:
        return float(config.settings().get("widow_share") or WIDOW_SHARE)
    except (TypeError, ValueError):
        return WIDOW_SHARE


def parse(text: str) -> dict:
    """rules.toml text -> a rules dict (ranges as tuples). RulesError names what is wrong."""
    try:
        raw = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError as err:
        raise RulesError(f"rules.toml is not valid TOML: {err}") from err
    unknown = sorted(set(raw) - set(DEFAULT_RULES) - {"verbs"})
    if unknown:
        raise RulesError(f"rules.toml: unknown key(s) {', '.join(unknown)}")
    out = {}
    for key, value in raw.items():
        if key in RANGES:
            if isinstance(value, int):
                value = [value, value]
            if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value)
                    and value[0] <= value[1]):
                raise RulesError(f"rules.toml: {key} must be [low, high], for example [15, 25]")
            out[key] = tuple(value)
        elif key in LISTS:
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise RulesError(f"rules.toml: {key} must be a list of strings")
            out[key] = tuple(value)
        elif key == "short_bullets":
            if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value)):
                raise RulesError("rules.toml: short_bullets must be [words, how many], for example [27, 4]")
            out[key] = tuple(value)
        elif key in ("verbs", "employer_domains"):
            if not (isinstance(value, dict) and all(isinstance(v, list) for v in value.values())):
                what = "verbs" if key == "verbs" else "domains"
                raise RulesError(f'rules.toml: [{key}] maps a company to a list of {what}, "Acme" = ["..."]')
            out[key] = {str(k): tuple(str(x) for x in v) for k, v in value.items()}
        else:
            out[key] = value
    if out.get("headline"):
        out.setdefault("headline_may_change", False)
    if out.get("headline_from_job"):
        out["headline_may_change"] = False
    return out


def load(path: Path) -> dict:
    """A profile's rules.toml (missing file = no rules of its own)."""
    try:
        return parse(Path(path).read_text())
    except FileNotFoundError:
        return {}


def for_profile(pid: str, profile_rules: dict | None = None) -> dict:
    """Rules for one person: DEFAULT_RULES <- their rules.toml. `profile_rules` is the parsed file (the Profile
    carries it); without it the file is read from the profile folder."""
    if profile_rules is None:
        from . import config
        try:
            profile_rules = load(config.PROFILES / pid / "rules.toml")
        except (RulesError, OSError):
            profile_rules = {}
    return {**DEFAULT_RULES, **profile_rules}


def banned_re(prules: dict) -> re.Pattern:
    extra = [r"\b" + re.escape(w) + r"\b" for w in prules.get("banned_words") or ()]
    return re.compile("|".join(BANNED + tuple(extra)), re.I) if extra else BANNED_RE


def summary_start(prules: dict, base_id: str | None) -> str | None:
    """The opening the summary must have: one string for every base, or a per-base table (base id -> opening)."""
    start = prules.get("summary_start")
    if isinstance(start, dict):
        return start.get(base_id or "")
    return start or None


def for_job(prules: dict, role: str | None, base_title: str, location: str | None = None) -> dict:
    """The person's rules with this job's title under the name ("headline") and the summary's opening, and the
    contact line's location (the job's city for a hybrid or on-site job; the person's own city otherwise).
    A new dict."""
    out = dict(prules)
    if location and prules.get("location") and location != prules["location"]:
        out = {**out, "home_location": prules["location"], "location": location}
    if not prules.get("headline_from_job"):
        if prules.get("headline") and prules.get("summary_years") and not prules.get("summary_start"):
            out["summary_start"] = f"{prules['headline']} with {prules['summary_years']}"
        return out
    fallback = prules.get("fallback_headline") or base_title
    title = headline_rule.for_job(role or "", fallback, prules.get("headline_titles"))
    years = prules.get("summary_years")
    return {**out, "headline": title, "summary_start": f"{title} with {years}" if years else prules.get("summary_start")}


def contact_location(prules: dict) -> tuple | None:
    """(the base's location, the job's) when the contact line must change, else None."""
    home, place = prules.get("home_location"), prules.get("location")
    return (home, place) if home and place and home != place else None


def headline_of(prules: dict, base_title: str) -> str:
    """The title under the name these rules require (the base's own title when no job headline is set)."""
    return prules.get("headline") or base_title


def is_era_job(company: str, rules: dict) -> bool:
    return any(company.lower().startswith(tag.lower()) for tag in rules.get("era_jobs") or ())
