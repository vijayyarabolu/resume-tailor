"""a needs-you card states why in plain words. The check messages are written for the repair loop
("bullet 4 of role 2 (Brightline Health, USA) is a widow…"); this turns them into what a person reads on a card:
an area ("Line fit") and the message with roles named by company and machine hints dropped."""
import re

AREAS = {1: "Page length", 2: "Layout", 3: "Fonts", 4: "Line fit", 5: "Honesty", 6: "ATS score", 7: "Writing",
         8: "Margins"}
_ROLE = re.compile(r"\b(bullet \d+) of role \d+ \(([^,)]+)(?:,[^)]*)?\)")
PLAIN = (
    (re.compile(r"^the PDF has (\d+) pages; it must be exactly 1.*$"), r"the resume runs onto \1 pages; it must fit on 1"),
    (re.compile(r"^text runs (\d+) px past the base's right margin on the line\(s\) ending (\"[^\"]*\").*$"),
     r"a line runs \1 px past the right edge: \2"),
    (re.compile(r"^text runs (\d+) px below the base's bottom margin.*$"), r"the page runs \1 px past the bottom edge"),
    (re.compile(r"^(.*?is a widow): its last line fills (\d+)% of the width.*$"),
     r"\1: its last line is only \2% wide (looks unfinished)"),
    (re.compile(r"^not in the base resumes or known skills: (.*)$"), r"skills you have not listed anywhere: \1"),
)


def plain_one(check: int, message: str) -> dict:
    text = _ROLE.sub(lambda m: f"{m.group(1)} at {m.group(2).strip()}", message.strip())
    for pattern, repl in PLAIN:
        new = pattern.sub(repl, text)
        if new != text:
            text = new
            break
    return {"area": AREAS.get(int(check or 0), "Check"), "text": text[:1].upper() + text[1:]}


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def own_edit(result: dict) -> list:
    """what the person's last edit could not do, first: a note that was not (fully) done, or a re-import
    edit the base layout cannot carry."""
    last = ((result.get("attempts") or [{}])[-1] or {}).get("kind")
    fix, back = result.get("fix") or {}, result.get("reimport") or {}
    out = []
    if last == "fix" and fix.get("not_done"):
        out.append({"area": "Your note", "text": _cap(f"not applied: {fix['not_done']}; the resume is as it was")})
    if last == "fix" and fix.get("cannot"):
        out.append({"area": "Your note", "text": _cap(f"not done: {fix['cannot']}")})
    if last == "reimport":
        out += [{"area": "Your edit", "text": _cap(x)} for x in back.get("not_kept") or []]
    return out


BEFORE_EDIT = "already there before your edit"


def _last_reimport(result: dict) -> dict:
    last = ((result.get("attempts") or [{}])[-1] or {}).get("kind")
    return (result.get("reimport") or {}) if last == "reimport" else {}


def _marked(item: dict, old: bool) -> dict:
    return {**item, "text": f"{item['text']} ({BEFORE_EDIT})", "old": True} if old else item


def plain(result: dict, error: str | None = None) -> list:
    """The person's own-edit problems, then the last round's failures as [{area, text}]; the job's error line when
    there is neither."""
    failures = (result.get("checks") or {}).get("failures") or []
    old = set(_last_reimport(result).get("old_problems") or [])
    out = own_edit(result) + [_marked(plain_one(f.get("check"), f.get("message", "")), f.get("message") in old)
                              for f in failures]
    if out:
        return out
    return [{"area": "Problem", "text": error}] if error else []


def what_changed(result: dict) -> str | None:
    """one line for the person's last edit (the panel, and a tracker through the API): "Your note: Added Data
    Warehousing to Skills (Data Engineering) · Northwind bullet 3" / "Your Doc edit: Location on the top line: Phoenix, AZ"."""
    last = ((result.get("attempts") or [{}])[-1] or {}).get("kind")
    fix, back = result.get("fix") or {}, result.get("reimport") or {}
    if last == "fix" and fix:
        if not fix.get("applied"):
            return f"Your note was not applied: {fix.get('not_done') or 'no change'}"
        return "Your note: " + (" · ".join(fix.get("changes") or []) or "no change")
    if last == "reimport" and back:
        return "Your Doc edit: " + " · ".join(back.get("changes") or [])
    return None

