"""Paths and settings. Stdlib only.

Everything the app writes lives in one folder outside the repo: ~/.resume-tailor by default, or whatever
RESUME_TAILOR_HOME points to. Settings go to settings.json there, keys and OAuth secrets to secrets.json (0600),
each person's resumes, fact bank and rules to profiles/<id>/.
"""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(__file__).resolve().parent
EXAMPLES = PACKAGE / "examples"


def _home() -> Path:
    return Path(os.environ.get("RESUME_TAILOR_HOME") or (Path.home() / ".resume-tailor")).expanduser()


DATA = _home()
RUNS = DATA / "runs"
PROFILES = DATA / "profiles"
CONFIG = DATA
SETTINGS_PATH = DATA / "settings.json"
SECRETS_PATH = DATA / "secrets.json"
DB_PATH = DATA / "resume-tailor.db"
SOCK_PATH = DATA / "worker.sock"
TOKEN_PATH = DATA / "token"



def set_home(path) -> None:
    """Point every path at another home folder (the demo command and the tests use this)."""
    global DATA, RUNS, PROFILES, CONFIG, SETTINGS_PATH, SECRETS_PATH, DB_PATH, SOCK_PATH, TOKEN_PATH
    DATA = Path(path).expanduser()
    RUNS, PROFILES, CONFIG = DATA / "runs", DATA / "profiles", DATA
    SETTINGS_PATH, SECRETS_PATH = DATA / "settings.json", DATA / "secrets.json"
    DB_PATH, SOCK_PATH, TOKEN_PATH = DATA / "resume-tailor.db", DATA / "worker.sock", DATA / "token"


CLAUDE_BACKENDS = ("api", "cli", "demo")
PDF_ENGINES = ("auto", "libreoffice", "word", "google")

DEFAULTS = {
    "port": 8770,
    "concurrency": 1,                   # jobs running at once per person
    "claude_backend": "api",            # api = Anthropic API key, cli = Claude Code on this machine, demo = no AI
    "model": "claude-sonnet-5-5",       # any Claude model id; the Settings page offers the current ones
    "effort": None,                     # None = the model's default; else low|medium|high|xhigh|max
    "max_attempts": 3,                  # full tailoring attempts (only when Claude gave no usable resume)
    "max_repairs": 5,                   # repair rounds after a full attempt: Claude rewrites only the failing items
    "max_claude_calls": 3,              # Claude calls per resume (full + repair), then it waits for you
    "max_tokens": 16000,                # per API call
    "claude_bin": "claude",
    "claude_timeout_s": 900,
    "max_budget_usd": 6.0,              # per `claude -p` call (CLI backend on API billing only)
    "pdf_engine": "auto",               # auto = LibreOffice, else Word (Mac), else Google Docs
    "soffice_bin": None,                # None = find LibreOffice on PATH or in /Applications
    "word_fallback": False,             # True: Microsoft Word may render after Google Docs fails
    "drive_root": ["Resume Tailor", "Approved"],   # approved PDFs: <drive_root>/<first name>/
    "drive_tmp": ["Resume Tailor", "tmp"],         # scratch Google Docs for PDF export (deleted after use)
    "drive_docs": ["Resume Tailor", "Docs"],       # "Open as Google Doc" copies
    "pause_default_min": 30,            # when a rate-limit message carries no reset time
    "login_recheck_min": 5,             # how often a missing Claude login is re-checked
    "allow_lan": False,                 # True also answers LAN clients (default: this machine only)
    "pdf_dpi": 110,                     # page images for the phone view
    "widow_share": 0.20,                # a bullet's last line under this share of the width is a widow
    "default_profile": None,            # whose page opens first (None = the first profile)
    "archived_delete_days": 30,         # archived resumes older than this are deleted for good (0 = never)
    "tracker_url": None,                # optional link to your job tracker in the side menu
}


def settings() -> dict:
    """DEFAULTS merged with settings.json (missing file = defaults). RESUME_TAILOR_DEMO=1 forces demo mode."""
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as err:
        raise ValueError(f"{SETTINGS_PATH} is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise ValueError(f"{SETTINGS_PATH} must hold a JSON object")
    merged = {**DEFAULTS, **data}
    if os.environ.get("RESUME_TAILOR_DEMO") == "1":
        merged["claude_backend"] = "demo"
    return merged


def save_settings(changes: dict) -> dict:
    """Write changed keys into settings.json."""
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    merged = {**data, **changes}
    ensure_dirs()
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2) + "\n")
    return settings()


def write_private(path: Path, text: str) -> None:
    """Write a file only this user can read (0600), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def secrets() -> dict:
    """Keys and OAuth client details (secrets.json, 0600). Never sent to the browser in full."""
    try:
        data = json.loads(SECRETS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_secret(key: str, value) -> None:
    data = secrets()
    if value in (None, "", {}):
        data.pop(key, None)
    else:
        data[key] = value
    write_private(SECRETS_PATH, json.dumps(data, indent=2) + "\n")


def anthropic_key() -> str | None:
    """The key saved in Settings, else ANTHROPIC_API_KEY from the environment."""
    return (secrets().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def ensure_dirs() -> None:
    for folder in (DATA, RUNS, PROFILES):
        folder.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)


def soffice(settings_: dict | None = None) -> str | None:
    """Path of LibreOffice's soffice, or None."""
    wanted = (settings_ or {}).get("soffice_bin")
    for name in ([wanted] if wanted else []) + ["soffice", "libreoffice"]:
        found = shutil.which(name)
        if found:
            return found
    mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return str(mac) if mac.exists() else None
