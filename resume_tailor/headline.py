"""The title under the name.

The headline is always exactly one of TITLES, picked by the posting title's family (not by which base won):
  - Data Scientist / Decision Science / analytics-science roles      -> "Data Scientist"
  - Data Engineer / Analytics Engineer / data platform / ETL          -> "Data Engineer"
  - a title naming both AI and ML ("AI/ML Engineer", "Machine Learning & AI Engineer") -> "AI/ML Engineer"
  - ML / Machine Learning / MLOps / Applied Scientist / Deep Learning / Computer Vision / NLP -> "Machine Learning
    Engineer"
  - AI / GenAI / LLM / Applied AI / AI Tools / Agentic / AI Product   -> "AI Engineer"
  - anything else: the fallback (the picked base's own title).
The data roles are checked first: a "Data Scientist, AI/ML" posting is a data scientist's job.

Pure functions, no I/O: prompts, checks, repair, pipeline and outdated all ask the same question and get the same answer.
"""
import re

AI_ENGINEER = "AI Engineer"
ML_ENGINEER = "Machine Learning Engineer"
DATA_SCIENTIST = "Data Scientist"
DATA_ENGINEER = "Data Engineer"
AI_ML_ENGINEER = "AI/ML Engineer"
TITLES = (AI_ENGINEER, ML_ENGINEER, DATA_SCIENTIST, DATA_ENGINEER, AI_ML_ENGINEER)


def _words(*patterns: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])(" + "|".join(patterns) + r")(?![a-z0-9])", re.I)


DATA_SCIENCE = _words(r"data scien\w*", r"decision scien\w*", r"analytics scien\w*", r"statistician")
DATA_ENGINEERING = _words(r"data engineer\w*", r"analytics engineer\w*", r"data platforms?", r"etl", r"elt",
                          r"data pipelines?", r"data warehous\w*", r"data infrastructure", r"big data")
ML = _words(r"ml", r"machine[\s-]learning", r"mlops", r"applied scientists?", r"deep[\s-]learning",
            r"computer vision", r"nlp", r"natural language processing")
AI = _words(r"ai", r"a\.i\.?", r"artificial intelligence", r"gen\s?ai", r"generative", r"llms?", r"llmops",
            r"agentic", r"ai tools", r"applied ai")


def for_job(title: str, fallback: str) -> str:
    """The headline for a posting titled `title`: one of TITLES by its family, else `fallback`."""
    text = (title or "").replace(" ", " ")
    if DATA_SCIENCE.search(text):
        return DATA_SCIENTIST
    if DATA_ENGINEERING.search(text):
        return DATA_ENGINEER
    has_ai, has_ml = bool(AI.search(text)), bool(ML.search(text))
    if has_ai and has_ml:
        return AI_ML_ENGINEER
    if has_ml:
        return ML_ENGINEER
    if has_ai:
        return AI_ENGINEER
    return fallback


# File names follow the role too, so the download, the Drive file and any tracker that keeps the name agree.
FILE_TITLES = {AI_ENGINEER: "AI_Engineer", ML_ENGINEER: "ML_Engineer", DATA_SCIENTIST: "Data_Scientist",
               DATA_ENGINEER: "Data_Engineer", AI_ML_ENGINEER: "AI_ML_Engineer"}


def file_stem(person: str, base_stem: str, headline: str | None, other_stems=()) -> str:
    """`base_stem` with its title swapped for `headline`'s: the person's own base with that title when they have one
    (Alex_Rivera_ML_Engineer_Resume), else their naming style with the new title (Sam_Lee_ML_Engineer_Resume).
    A headline outside TITLES, or a stem that does not start with the person's name: `base_stem` unchanged."""
    token = FILE_TITLES.get(headline or "")
    if not token:
        return base_stem
    ends_with = re.compile(rf"(?:^|_)(?<!AI_){re.escape(token)}(?:_Resume)?$", re.I)
    if ends_with.search(base_stem):
        return base_stem
    for other in other_stems:
        if ends_with.search(other):
            return other
    who = re.sub(r"\s+", "_", (person or "").strip())
    if not who or not base_stem.lower().startswith(who.lower() + "_"):
        return base_stem
    return f"{base_stem[:len(who)]}_{token}{'_Resume' if base_stem.lower().endswith('_resume') else ''}"
