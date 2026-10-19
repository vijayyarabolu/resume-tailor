"""Where the job is, for the contact line. A request is never refused for its location: a place that isn't
"City, ST" yet is tidied or read out of an address, and when it still can't be worked out the resume waits in
Queue as "needs you: pick the location" with the cities found.

place(jd_text) reads the workplace (remote, hybrid, onsite) and the city from a pasted job description.
US-style "City, ST" only; anywhere else the person's own city stays on the resume."""
import re

from . import rules

LOCATION = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,40}, [A-Z]{2}$")       # "Wilmington, MA"
_LOOSE = re.compile(r"^\s*([A-Za-z][A-Za-z .'-]{0,40}?)\s*,\s*([A-Za-z]{2})\s*$")    # "Brooklyn Park,MN", "austin , tx"
REMOTE = "remote"
NEEDS = "needs you: pick the location"
STATES = frozenset("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
                   "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())
# a city is one to four capitalised words right before ", ST" (an address's street part is cut at the last comma)
_CITY_IN_TEXT = re.compile(r"(?:(?<=[,;:(\n])|^|(?<=\s))\s*((?:[A-Z][a-zA-Z.'-]*\s){0,3}[A-Z][a-zA-Z.'-]*)\s*,\s*"
                           r"([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?")
_STREET = re.compile(r"\d|\b(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Pkwy|Parkway|Dr|Drive|Suite|Ste|Hwy|Way|Ln|Lane)\b\.?")
_REMOTE_RE = re.compile(r"\b(fully remote|remote[- ]first|100% remote|remote \(|work from home|remote(?:,| -| –| only)|"
                        r"location:?\s*remote)\b|^\s*remote\b", re.I | re.M)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]office|in the office|in-person)\b", re.I)
_LOCATION_LINE = re.compile(r"^\s*(?:location|locations|office|based in|where)\s*[:\-]\s*(.+)$", re.I | re.M)


def home(pid: str) -> str | None:
    """The person's own city (the Remote choice): their rules' location."""
    return rules.for_profile(pid).get("location")


def _tidy(text: str) -> str | None:
    text = " ".join(str(text or "").split())
    if LOCATION.match(text) and text[-2:] in STATES:
        return text
    loose = _LOOSE.match(text)
    if loose and loose.group(2).upper() in STATES:
        city = loose.group(1).title() if loose.group(1).islower() else loose.group(1)
        place = f"{city}, {loose.group(2).upper()}"
        if LOCATION.match(place):
            return place
    return None


def city(text: str) -> dict:
    """{location: "City, ST" when the text names exactly one, else None; choices: every city it names}."""
    choices = []
    for chunk in re.split(r"[;|/\n]|\bor\b", text or ""):
        for m in _CITY_IN_TEXT.finditer(chunk):
            name, state = m.group(1).strip(), m.group(2)
            if state not in STATES:
                continue
            words = name.split()
            while words and _STREET.search(words[0]):
                words = words[1:]
            place = f"{' '.join(words)}, {state}"
            if words and LOCATION.match(place) and place not in choices:
                choices.append(place)
    return {"location": choices[0] if len(choices) == 1 else None, "choices": choices}


def normalise(text: str | None) -> tuple[str | None, list[str]]:
    """("City, ST" or None when unclear, the cities the text names)."""
    tidy = _tidy(text or "")
    if tidy:
        return tidy, [tidy]
    found = city(" ".join(str(text or "").split()))
    choices = [c for c in found.get("choices") or [] if isinstance(c, str) and LOCATION.match(c)]
    if len(choices) > 1:                                    # several cities: the person picks (never the first one)
        return None, choices
    place = found.get("location")
    return (place if isinstance(place, str) and LOCATION.match(place) else None), choices


def pick(text: str | None) -> tuple[str, str | None]:
    """The person's pick -> (workplace override or "", location): REMOTE -> ("remote", None); a city -> ("", "City, ST").
    ValueError when it can't be read."""
    raw = " ".join(str(text or "").split())
    if raw.lower() == REMOTE:
        return REMOTE, None
    place, _ = normalise(raw)
    if place is None:
        raise ValueError(f"Type the city as 'Brooklyn Park, MN' (got {raw!r})")
    return "", place


def place(jd_text: str) -> dict:
    """{workplace: remote | hybrid | onsite | None, location: "City, ST" | None, why} read from a job description.
    A remote job keeps the person's own city (location None)."""
    text = jd_text or ""
    head = "\n".join(text.splitlines()[:25])
    workplace = ("hybrid" if _HYBRID_RE.search(text) else "remote" if _REMOTE_RE.search(text)
                 else "onsite" if _ONSITE_RE.search(text) else None)
    if workplace == "remote":
        return {"workplace": "remote", "location": None, "why": "remote: your own city stays on the resume"}
    lines = [m.group(1) for m in _LOCATION_LINE.finditer(text)]
    for source in lines + [head]:
        found = city(source)
        if found["location"]:
            where = found["location"]
            return {"workplace": workplace or "onsite", "location": where,
                    "why": f"{workplace or 'on-site'}: the job's city {where}"}
    return {"workplace": workplace, "location": None, "why": "no single city found in the job description"}
