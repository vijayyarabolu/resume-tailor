"""What the tailoring checks need besides the plain rules (tailorchecks.py): the LOCKED skills, each skills row's DROP
ORDER, the anchor bullets, how many spare bullets a job may take, the industries the summary may name, and the spare
bullets themselves. The first five come from rules.toml; the spares come from facts.md, under a "## Part 3" heading:

    ## Part 3: spare bullets (true, approved, swapped in without asking)

    ### Lumen Freight (spare bullets)
    - L1 (evaluation, quality): Ran a 600 question evaluation set on every release of the dispatcher tool ...
    - L2 (variant of the drift bullet; monitoring): Paged the on-call engineer within 10 minutes of ...
    - L3 (cost; replaces SageMaker or training bullet): Moved batch scoring to spot instances ...

Each spare is "- <id> (<what it covers>): <the bullet>", under "### <employer>". "variant of X" or "replaces X or Y"
in the brackets names the only base bullets it may take the place of.
"""
import re
from dataclasses import dataclass, field

DEFAULT_MAX_SWAPS = 2
_ITEM_SPLIT = re.compile(r",(?![^()]*\))")              # commas outside parentheses: "AWS (SageMaker, Lambda)" is one
_PART3 = re.compile(r"^## Part 3\b.*?$(.*?)(?=^## |\Z)", re.M | re.S)
_SPARE = re.compile(r"^\s*-\s+([A-Z]\d+)\s+\(([^)]*)\):\s*(.+?)\s*$", re.M)
_VARIANT = re.compile(r"variant of (?:the )?(.+?)(?:\s+bullet)?\s*$", re.I)
_REPLACES = re.compile(r"replaces (.+?)(?:\s+bullets?)?\s*$", re.I)


@dataclass(frozen=True)
class Spare:
    id: str
    job: str                      # the employer heading it sits under, e.g. "Lumen Freight"
    tags: str                     # what the spare covers, as written
    text: str
    targets: tuple = ()           # "variant of X" / "replaces X or Y": the only base bullets it may replace


@dataclass(frozen=True)
class SkillRules:
    locked: tuple = ()
    drop_order: dict = field(default_factory=dict)      # row name -> (item, ...) first out first
    anchors: tuple = ()
    max_swaps: int = DEFAULT_MAX_SWAPS
    industries: tuple = ()
    spares: tuple = ()

    def spares_of(self, company: str) -> tuple:
        return tuple(s for s in self.spares if same_employer(s.job, company))


def items(text: str) -> list:
    """'a, b (c, d), e' -> ['a', 'b (c, d)', 'e'] (whitespace and line breaks squashed)."""
    flat = " ".join((text or "").split())
    return [x.strip().rstrip(".") for x in _ITEM_SPLIT.split(flat) if x.strip().rstrip(".")]


def employer_word(name: str) -> str:
    """'Lumen Freight, USA' -> 'lumen'; 'AT&T Labs' -> 'at&t'."""
    head = (name or "").split(",")[0].strip()
    return head.split()[0].lower() if head else ""


def same_employer(a: str, b: str) -> bool:
    return bool(employer_word(a)) and employer_word(a) == employer_word(b)


def _targets(tags: str) -> tuple:
    for part in (p.strip() for p in tags.split(";")):
        m = _VARIANT.match(part) or _REPLACES.search(part)
        if m:
            return tuple(t.strip().strip('"') for t in re.split(r"\s+or\s+", m.group(1)) if t.strip())
    return ()


def _heading(line: str):
    return re.sub(r"\s*\(.*\)\s*$", "", line[4:]).strip() if line.startswith("### ") else None


def spares(fact_bank: str) -> tuple:
    """The spare bullets of facts.md Part 3, each under its employer's '### <employer> ...' heading."""
    m = _PART3.search(fact_bank or "")
    if not m:
        return ()
    out, job = [], ""
    for line in m.group(1).splitlines():
        head = _heading(line)
        if head is not None:
            job = head
            continue
        hit = _SPARE.match(line)
        if hit and job:
            out.append(Spare(hit.group(1), job, hit.group(2).strip(), hit.group(3).strip(), _targets(hit.group(2))))
    return tuple(out)


def for_rules(prules: dict, fact_bank: str = "") -> SkillRules:
    """The person's rules.toml keys (locked_skills, [drop_order], anchor_bullets, max_swaps, summary_industries) and
    the spares of their facts.md."""
    cap = prules.get("max_swaps")
    return SkillRules(tuple(prules.get("locked_skills") or ()),
                      {str(k): tuple(v) for k, v in (prules.get("drop_order") or {}).items()},
                      tuple(prules.get("anchor_bullets") or ()),
                      DEFAULT_MAX_SWAPS if cap is None else int(cap),
                      tuple(x.lower() for x in prules.get("summary_industries") or ()),
                      spares(fact_bank))
