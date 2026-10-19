"""what an edit changed, in the words a person reads in the panel, and how a person's own contact-line
edit is carried into the base layout.

describe(old, new)  -> ["Added Data Warehousing to Skills (Data Engineering)", "Northwind bullet 3", ...]
contact_change(...) -> the location / other contact segments the person changed in their Google Doc
not_kept(old, new)  -> edits a re-import cannot carry into the base layout (titles, dates, Education)
"""
import re

SEP = "|"


def _items(line: dict) -> list:
    items = line.get("items") or []
    return [i.strip() for i in (items if isinstance(items, list) else str(items).split(",")) if str(i).strip()]


def _words(text: str | None) -> int:
    return len(re.findall(r"\S+", text or ""))


def short_company(company: str) -> str:
    """"Northwind Analytics, USA" -> "Northwind Analytics"."""
    return (company or "").split(",")[0].strip() or "role"


def _skills(old: dict, new: dict) -> list:
    out = []
    for i, (a, b) in enumerate(zip(old.get("skills") or [], new.get("skills") or []), 1):
        before, after = _items(a), _items(b)
        plus = [x for x in after if x not in before]
        minus = [x for x in before if x not in after]
        name = b.get("category") or f"line {i}"
        if plus:
            out.append(f"Added {', '.join(plus)} to Skills ({name})")
        if minus:
            out.append(f"Removed {', '.join(minus)} from Skills ({name})")
        if not plus and not minus and (before != after or a.get("category") != b.get("category")):
            out.append(f"Skills line {i} ({name}) reordered")
    return out


def _summary(old: dict, new: dict) -> list:
    a, b = old.get("summary") or "", new.get("summary") or ""
    if a == b:
        return []
    wa, wb = _words(a), _words(b)
    how = "shortened" if wb < wa else "lengthened" if wb > wa else "reworded"
    return [f"Summary {how} ({wa} → {wb} words)" if how != "reworded" else "Summary reworded"]


def _bullets(old: dict, new: dict) -> list:
    out = []
    for a, b in zip(old.get("jobs") or [], new.get("jobs") or []):
        for k, (x, y) in enumerate(zip(a.get("bullets") or [], b.get("bullets") or []), 1):
            if x != y:
                out.append(f"{short_company(b.get('company') or a.get('company'))} bullet {k}")
    return out


def describe(old: dict | None, new: dict | None) -> list:
    """Plain lines for what changed between two resume contents (empty: nothing)."""
    old, new = old or {}, new or {}
    out = []
    if (new.get("headline") or "") != (old.get("headline") or "") and new.get("headline"):
        out.append(f"Title under the name: {new['headline']}")
    if (new.get("user_location") or None) != (old.get("user_location") or None) and new.get("user_location"):
        out.append(f"Location on the top line: {new['user_location']}")
    for a, b in (new.get("contact_edits") or []):
        if [a, b] not in [list(e) for e in old.get("contact_edits") or []]:
            out.append(f"Contact line: {a} → {b}")
    return out + _summary(old, new) + _skills(old, new) + _bullets(old, new)


def _segments(text: str) -> list:
    return [s.strip() for s in (text or "").split(SEP)]


def contact_change(shown: str, edited: str, location: str | None) -> tuple:
    """(user location or None, [[old, new], ...] other segments, problem or None) for a person's edit of the
    contact line. `shown` is the line as resume_tailor made it, `location` the place written on it."""
    if " ".join(shown.split()) == " ".join(edited.split()):
        return None, [], None
    a, b = _segments(shown), _segments(edited)
    if len(a) != len(b):
        return None, [], (f"your contact line edit (\"{edited.strip()}\") could not be placed into the base layout; "
                          "the line stays as it was")
    user_location, edits = None, []
    for old, new in zip(a, b):
        if old == new:
            continue
        if location and old == location:
            user_location = new
        else:
            edits.append([old, new])
    return user_location, edits, None


def not_kept(old, new) -> list:
    """Edits a re-import cannot carry (the base layout keeps job titles, dates and the Education lines): one plain
    line each, so the person is told instead of silently losing them. `old` / `new` are docxio.Structure."""
    out = []
    for a, b in zip(old.jobs, new.jobs):
        name = short_company(a["company"])
        if " ".join(a["title"].split()) != " ".join(b["title"].split()):
            out.append(f"{name} job title (\"{b['title']}\") was not kept: the base layout keeps titles")
        if " ".join(a["dates"].split()) != " ".join(b["dates"].split()) or a["company"] != b["company"]:
            out.append(f"{name} company / dates line was not kept: the base layout keeps it")
    if [" ".join(e.split()) for e in old.education] != [" ".join(e.split()) for e in new.education]:
        out.append("the Education lines were not kept: the base layout keeps them")
    if old.sections != new.sections:
        out.append("section headings or their order were not kept: the base layout keeps them")
    return out
