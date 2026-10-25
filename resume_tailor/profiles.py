"""The people and their base resumes. One folder per person under <home>/profiles/<id>/:

    profile.toml        name, years of experience, optional labels for the base resumes
    resumes/*.docx      the base resumes (a sibling .pdf is optional; the PDF engine renders one when missing)
    facts.md            the fact bank and writing notes: the only facts Claude may use
    known_skills.txt    skills the person can claim (one per line) beyond what the base resumes list
    rules.toml          the rules the checks enforce (see rules.py)

Byte-identical duplicate .docx files are collapsed. Labels come from profile.toml `[[resumes]]` when listed, else
from the file name (Alex_Rivera_ML_Engineer.docx -> "ML Engineer").
"""
import hashlib
import re
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from . import config, confirmed, docxio, rules as rules_mod

PID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
FILES = ("profile.toml", "facts.md", "known_skills.txt", "rules.toml")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Base:
    id: str
    label: str
    docx: Path
    pdf: Path | None
    families: tuple
    sha: str
    structure: docxio.Structure

    @property
    def text(self) -> str:
        return self.structure.full_text()


@dataclass
class Profile:
    id: str
    name: str
    headline_default: str
    years: int | None
    bases: list = field(default_factory=list)
    known_skills: list = field(default_factory=list)
    skill_path: Path | None = None                     # facts.md
    skill_text: str = ""
    rules: dict = field(default_factory=dict)          # parsed rules.toml
    anchors: list = field(default_factory=list)
    file_pattern: str = "{name}_{title}_Resume({company}).pdf"
    share_with: str | None = None
    config_dir: str | None = None                      # CLAUDE_CONFIG_DIR for the CLI backend (None = default)
    raw: dict = field(default_factory=dict)
    fact_bank: str = ""                                # facts.md as written (without confirmed notes)

    @property
    def fact_bank_resume(self) -> str:
        """facts.md up to a '## Part 2' heading, plus Part 3 (the spare bullets) when there is one. With rules.toml
        `fact_bank_numbers` a number on the page must come from here or the base resume; Part 2 (interview-only facts)
        shapes wording only."""
        cut = re.search(r"^## Part 2\b", self.fact_bank, re.M)
        if not cut:
            return self.fact_bank
        rest = re.search(r"^## Part 3\b", self.fact_bank[cut.end():], re.M)
        return self.fact_bank[:cut.start()] + (self.fact_bank[cut.end() + rest.start():] if rest else "")

    def base(self, base_id: str | None):
        return next((b for b in self.bases if b.id == base_id), None)

    def known_text(self) -> str:
        return "\n".join(self.known_skills)


def folder(pid: str) -> Path:
    if not PID.match(pid or ""):
        raise ProfileError(f"bad profile id {pid!r}: lowercase letters, digits, - and _ only")
    return config.PROFILES / pid


def _label_from_file(name: str, person: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"^" + re.escape(person.replace(" ", "_")) + r"_?", "", stem, flags=re.I)
    stem = re.sub(r"_?Resume$", "", stem, flags=re.I)
    return stem.replace("_", " ").strip() or Path(name).stem


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as err:
        raise ProfileError(f"{path.name} is not valid TOML: {err}") from err


def _bases(pid: str, raw: dict) -> list[Base]:
    where = folder(pid) / "resumes"
    listed = {str(e["file"]): e for e in (raw.get("resumes") or []) if isinstance(e, dict) and e.get("file")}
    out, seen = [], {}
    for docx in sorted(where.glob("*.docx")):
        if docx.name.startswith("~$"):
            continue
        pdf = docx.with_suffix(".pdf")
        data = docx.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen:                                   # a byte-identical copy under another name
            continue
        entry = listed.get(docx.name)
        label = str(entry.get("label")) if entry and entry.get("label") else _label_from_file(docx.name, raw["name"])
        bid = str(entry["id"]) if entry and entry.get("id") else _slug(label)
        doc = docxio.read(data)
        struct = docxio.structure(doc)
        problems = docxio.validate_structure(struct)
        if problems:
            raise ProfileError(f"{docx.name}: {'; '.join(problems)}")
        base = Base(bid, label, docx, pdf if pdf.exists() else None,
                    tuple(entry.get("families") or ()) if entry else (), sha, struct)
        seen[sha] = base
        out.append(base)
    order = {name: n for n, name in enumerate(listed)}
    out.sort(key=lambda b: (order.get(b.docx.name, 10 ** 6), b.label))
    return out


def _known(pid: str) -> list:
    try:
        lines = (folder(pid) / "known_skills.txt").read_text().splitlines()
    except FileNotFoundError:
        return []
    return [ln.split("#")[0].strip() for ln in lines if ln.split("#")[0].strip()]


def load(pid: str) -> Profile:
    """The person's profile with what they confirmed in notes added: their confirmed skills count as known skills
    and their confirmed facts follow the fact bank. A new Profile each call (the lists are tiny)."""
    prof = _load(pid, _stamp(pid))
    skills = confirmed.read(pid, "skills")
    facts = confirmed.facts_text(pid, prof.name)
    if not skills and not facts:
        return prof
    known = {k.lower() for k in prof.known_skills}
    return replace(prof, known_skills=prof.known_skills + [s for s in skills if s.lower() not in known],
                   skill_text="\n\n".join(t for t in (prof.skill_text, facts) if t))


def _stamp(pid: str) -> tuple:
    """Changes whenever a file of the profile changes, so an edit on the Settings page is seen at once."""
    base = folder(pid)
    paths = [base / name for name in FILES] + sorted((base / "resumes").glob("*.docx"))
    out = [str(base)]
    for p in paths:
        try:
            st = p.stat()
            out.append((p.name, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((p.name, None, None))
    return tuple(out)


@lru_cache(maxsize=16)
def _load(pid: str, stamp: tuple) -> Profile:
    raw = read_toml(folder(pid) / "profile.toml")
    if not raw.get("name"):
        raise ProfileError(f"profile {pid}: profile.toml has no name")
    skill_path = folder(pid) / "facts.md"
    try:
        skill_text = skill_path.read_text()
    except OSError:
        skill_text = ""
    try:
        prules = rules_mod.load(folder(pid) / "rules.toml")
    except rules_mod.RulesError as err:
        raise ProfileError(str(err)) from err
    bases = _bases(pid, raw)
    if not bases:
        raise ProfileError(f"profile {pid}: no base resume (.docx) in profiles/{pid}/resumes/")
    years = raw.get("years")
    return Profile(pid, str(raw["name"]), str(raw.get("headline_default") or "Resume"),
                   int(years) if isinstance(years, int) else None, bases, _known(pid), skill_path, skill_text, prules,
                   list(raw.get("summary_anchors") or []), str(raw.get("file_pattern") or Profile.file_pattern),
                   None, raw.get("claude_config_dir") or None, raw, skill_text)


def ids() -> list[str]:
    """Profiles that have at least one base resume."""
    if not config.PROFILES.exists():
        return []
    return sorted(p.name for p in config.PROFILES.iterdir()
                  if p.is_dir() and PID.match(p.name) and any((p / "resumes").glob("*.docx")))


def reload() -> None:
    _load.cache_clear()


def install_example(name: str = "alex-rivera", pid: str | None = None) -> str:
    """Copy a bundled example profile into the profiles folder (never over an existing one). Returns its id."""
    source = config.EXAMPLES / name
    if not source.is_dir():
        raise ProfileError(f"no example profile {name!r}")
    pid = pid or name
    target = folder(pid)
    if target.exists():
        return pid
    config.ensure_dirs()
    shutil.copytree(source, target)
    reload()
    return pid


def create(pid: str, name: str) -> str:
    """A new, empty profile: profile.toml, an empty fact bank and rules file. The base resume is uploaded next."""
    target = folder(pid)
    if target.exists():
        raise ProfileError(f"profile {pid} already exists")
    if not name.strip():
        raise ProfileError("a profile needs a name")
    (target / "resumes").mkdir(parents=True)
    escaped = name.strip().replace("\\", "\\\\").replace('"', '\\"')
    (target / "profile.toml").write_text(f'name = "{escaped}"\n# years = 5\n')
    (target / "facts.md").write_text(FACTS_TEMPLATE)
    (target / "known_skills.txt").write_text("# one skill per line\n")
    (target / "rules.toml").write_text(RULES_TEMPLATE)
    reload()
    return pid


def read_file(pid: str, name: str) -> str:
    if name not in FILES:
        raise ProfileError(f"unknown profile file {name!r}")
    try:
        return (folder(pid) / name).read_text()
    except FileNotFoundError:
        return ""


def write_file(pid: str, name: str, text: str) -> None:
    """Save one of the profile's text files after checking it parses."""
    if name not in FILES:
        raise ProfileError(f"unknown profile file {name!r}")
    if name == "rules.toml":
        try:
            rules_mod.parse(text)
        except rules_mod.RulesError as err:
            raise ProfileError(str(err)) from err
    if name == "profile.toml":
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as err:
            raise ProfileError(f"profile.toml is not valid TOML: {err}") from err
        if not parsed.get("name"):
            raise ProfileError('profile.toml needs a name, for example: name = "Alex Rivera"')
    target = folder(pid)
    if not target.is_dir():
        raise ProfileError(f"no profile {pid}")
    (target / name).write_text(text)
    reload()


def add_resume(pid: str, filename: str, data: bytes) -> str:
    """Store an uploaded base resume after checking the layout can be read. Returns the saved file name."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename or "resume.docx").stem).strip("._") or "resume"
    try:
        struct = docxio.structure(docxio.read(data))
    except docxio.DocxError as err:
        raise ProfileError(str(err)) from err
    problems = docxio.validate_structure(struct)
    if problems:
        raise ProfileError("this .docx can't be used as a base resume: " + "; ".join(problems)
                           + ". See docs/BASE_RESUME.md for the layout it needs.")
    where = folder(pid) / "resumes"
    if not where.parent.is_dir():
        raise ProfileError(f"no profile {pid}")
    where.mkdir(exist_ok=True)
    (where / f"{stem}.docx").write_bytes(data)
    stale = where / f"{stem}.pdf"
    stale.unlink(missing_ok=True)                 # an old PDF of a replaced file would mislead the measure
    reload()
    return f"{stem}.docx"


FACTS_TEMPLATE = """# Fact bank

Everything here is true and may be used in a tailored resume. Claude rewrites bullets only with facts from this
file and the base resume; a number that is in neither fails the honesty check.

## Roles

### Company name (Mon YYYY to Mon YYYY)
- What you built, with the real numbers (latency, cost, users, accuracy).

## Writing notes
- Plain verbs, no buzzwords.
"""

RULES_TEMPLATE = """# The rules the checks enforce for this person. Every key is optional; see rules.py for the full list.
bullet_words = [15, 28]
summary_words = [40, 80]
min_bullet_lines = 2
jd_skills_allowed = false
"""
