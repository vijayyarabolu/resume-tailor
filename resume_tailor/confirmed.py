"""what a person confirmed in a "Fix with note" is theirs to claim.

A skill the note names ("I know data warehousing") passes the honesty check for that job, and is saved to the
person's own list so later resumes may use it too; a note that states a number ("cut costs 30%") becomes a fact the
same way. The lists live in <home>/confirmed/ (the profile's own files are never rewritten), one line per entry:
    Data Warehousing  # [from note, job 108, 2026-10-12]
"""
import re
from datetime import datetime
from pathlib import Path

from . import config

KINDS = ("skills", "facts")
_PID = re.compile(r"^[a-z0-9_-]{1,40}$")
_STEM = 6                     # "warehouse" and "warehousing" share "wareho"
_MIN_WORD = 3
_COMMENT = "  # ["


def path(pid: str, kind: str) -> Path:
    if not _PID.match(pid or "") or kind not in KINDS:
        raise ValueError(f"bad person / kind: {pid!r} {kind!r}")
    return config.DATA / "confirmed" / f"{pid}.{kind}.txt"


def read(pid: str, kind: str) -> list:
    """The entries (comments dropped); [] when the person confirmed nothing yet."""
    try:
        lines = path(pid, kind).read_text().splitlines()
    except (OSError, ValueError):
        return []
    return [line.split(_COMMENT)[0].strip() for line in lines if line.strip() and not line.startswith("#")]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", text.lower())


def add(pid: str, kind: str, entries, job_id: int) -> list:
    """Appends the entries that are not on the list yet; returns those added."""
    have = {_key(e) for e in read(pid, kind)}
    new, seen = [], set()
    for entry in entries:
        text = " ".join(str(entry).split())
        if text and _key(text) not in have | seen:
            new.append(text)
            seen.add(_key(text))
    if not new:
        return []
    target = path(pid, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    with target.open("a") as fh:
        for text in new:
            fh.write(f"{text}{_COMMENT}from note, job {int(job_id)}, {day}]\n")
    return new


_SHORT = 3                    # a term this short ("R", "Go") must appear as its own word, in its own case
_CLAUSE = re.compile(r"[.;,!?\n]|\bbut\b", re.I)
_NEGATIVE = re.compile(r"\b(?:never|not|no|don'?t|didn'?t|haven'?t|hasn'?t|isn'?t|without|remove|drop|delete|"
                       r"take (?:it |that |them )?out|get rid)\b", re.I)
_REFERENCE = re.compile(r"\b(?:bullets?|lines?|roles?|jobs?|items?|pages?|points?|sentences?|words?|skills? lines?)"
                        r"\s*#?\s*\d+(?:\s*(?:and|,|-|to)\s*\d+)*|\b\d+(?:st|nd|rd|th)\b|\b\d+\s*(?:words?|lines?|pages?)\b",
                        re.I)


def _words(text: str) -> list:
    return [w for w in re.findall(r"[a-z0-9+#]+", text.lower()) if len(w) >= _MIN_WORD or w.isdigit()]


def _written_out(term: str, clause: str) -> bool:
    parts = [re.escape(p) for p in re.findall(r"[A-Za-z0-9+#]+", term)]
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9+#])" + r"[\s\-_/.&]*".join(parts) + r"(?![A-Za-z0-9+#])"
    short = len("".join(re.findall(r"[A-Za-z0-9]", term))) <= _SHORT
    if not short:
        return bool(re.search(pattern, clause, re.I))
    lead = len(clause) - len(clause.lstrip())          # a clause's first word is capitalised anyway ("Go ahead")
    return any(m.start() != lead for m in re.finditer(pattern, clause))


def _by_stems(term: str, clause: str) -> bool:
    wanted = _words(term)
    if not wanted or len("".join(wanted)) <= _SHORT:
        return False
    have = {w[:_STEM] for w in _words(clause)}
    return all(w[:_STEM] in have for w in wanted)


def named_in_note(term: str, note: str) -> bool:
    """True when the note says the person has the term: written out as whole words ("Data Warehousing"; a short
    term like "R" or "Go" only as its own word in its own case), or every word of it by its stem ("data
    warehouse"), in a clause that does not deny or remove it ("I have never used Kafka, take it out")."""
    if not term or not note:
        return False
    for clause in _CLAUSE.split(note):
        if clause and (_written_out(term, clause) or _by_stems(term, clause)) and not _NEGATIVE.search(clause):
            return True
    return False


def fact_text(note: str) -> str:
    """The note without references to the resume itself ("bullet 3", "line 2", "2 pages", "25 words"): what is
    left is what the person states about themselves (its numbers count as theirs)."""
    return _REFERENCE.sub(" ", note or "")


def facts_text(pid: str, name: str) -> str:
    """The facts section added to the person's skill text (numbers in it count as the person's own)."""
    facts = read(pid, "facts")
    if not facts:
        return ""
    return "\n".join([f"=== FACTS {name.upper()} CONFIRMED IN NOTES (true; usable like the fact bank) ==="]
                     + [f"- {f}" for f in facts])
