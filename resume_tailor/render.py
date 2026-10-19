""".docx -> PDF. Three engines, picked by settings `pdf_engine`:

- libreoffice: `soffice --headless --convert-to pdf`, local and free (Linux, macOS, Windows).
- word: Microsoft Word through AppleScript (macOS with Word installed).
- google: upload as a Google Doc, export PDF, delete the temp Doc (needs Google connected in Settings).

"auto" tries them in that order, using only what this machine has. Every resume is checked against its base
rendered by the same engine, so the line budgets always match what the engine draws."""
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from . import config
from .gdocs import GoogleError

log = logging.getLogger(__name__)
WORD_APP = Path("/Applications/Microsoft Word.app")
WORD_TIMEOUT_S = 180
LO_TIMEOUT_S = 180
_WORD_LOCK = threading.Lock()
_LO_LOCK = threading.Lock()
_WORD_SCRIPT = """
on run argv
  set inPath to item 1 of argv
  set outPath to item 2 of argv
  tell application "Microsoft Word"
    set theDoc to open file name inPath
    save as theDoc file name outPath file format format PDF
    close theDoc saving no
  end tell
end run
"""


class RenderError(RuntimeError):
    pass


def word_available() -> bool:
    return WORD_APP.exists()


def libreoffice_available(settings: dict | None = None) -> bool:
    return config.soffice(settings) is not None


def word_to_pdf(docx_path: Path, pdf_path: Path) -> Path:
    """osascript tells Word to open, save as PDF, close. Word stays open afterwards if it was already running."""
    if not word_available():
        raise RenderError("Microsoft Word is not installed")
    try:
        proc = subprocess.run(["osascript", "-", str(docx_path), str(pdf_path)], input=_WORD_SCRIPT,
                              capture_output=True, text=True, timeout=WORD_TIMEOUT_S)
    except subprocess.TimeoutExpired as err:
        raise RenderError(f"Word did not finish in {WORD_TIMEOUT_S} s") from err
    if proc.returncode != 0 or not pdf_path.exists():
        raise RenderError(f"Word conversion failed: {proc.stderr.strip()[-300:]}")
    return pdf_path


def libreoffice_to_pdf(docx_bytes: bytes, settings: dict | None = None) -> bytes:
    """One headless conversion. LibreOffice keeps its own profile under <home>/libreoffice so it never touches a
    desktop LibreOffice the person has open; conversions run one at a time."""
    soffice = config.soffice(settings if settings is not None else config.settings())
    if soffice is None:
        raise RenderError("LibreOffice: not installed (install it from libreoffice.org, or pick another PDF engine)")
    profile = config.DATA / "libreoffice"
    profile.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="resume-tailor-lo-") as tmp:
        src = Path(tmp) / "resume.docx"
        src.write_bytes(docx_bytes)
        cmd = [soffice, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--norestore",
               "--nologo", "--convert-to", "pdf", "--outdir", tmp, str(src)]
        try:
            with _LO_LOCK:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=LO_TIMEOUT_S)
        except subprocess.TimeoutExpired as err:
            raise RenderError(f"LibreOffice did not finish in {LO_TIMEOUT_S} s") from err
        except OSError as err:
            raise RenderError(f"LibreOffice could not start: {err}") from err
        out = Path(tmp) / "resume.pdf"
        if not out.exists():
            raise RenderError(f"LibreOffice conversion failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
        return out.read_bytes()


def _google(docx_bytes: bytes, title: str, google) -> bytes:
    if google is None:
        raise RenderError("Google Docs: not connected")
    try:
        return google.docx_to_pdf(docx_bytes, title)
    except GoogleError as err:
        raise RenderError(f"Google Docs: {err}") from err


def _word(docx_bytes: bytes, work: Path, docx_path: Path | None) -> bytes:
    if not word_available():
        raise RenderError("Microsoft Word: not installed")
    docx_path = docx_path or (work / "resume.docx")
    if not docx_path.exists():
        docx_path.write_bytes(docx_bytes)
    pdf_path = work / "resume.word.pdf"
    with _WORD_LOCK:                      # one Word conversion at a time (two queues share one Word)
        word_to_pdf(docx_path, pdf_path)
    return pdf_path.read_bytes()


def render_with(engine: str, docx_bytes: bytes, work: Path, title: str = "resume", google=None,
                docx_path: Path | None = None) -> bytes:
    """One engine, no fallback."""
    if engine == "libreoffice":
        return libreoffice_to_pdf(docx_bytes)
    if engine == "google":
        return _google(docx_bytes, title, google)
    if engine == "word":
        return _word(docx_bytes, work, docx_path)
    raise RenderError(f"unknown PDF engine {engine!r}")


def to_pdf(docx_bytes: bytes, title: str, work: Path, google=None, docx_path: Path | None = None,
           order=("libreoffice",), validate=None) -> tuple:
    """(pdf bytes, engine). Engines in `order`; the first that works AND passes `validate(pdf_bytes)` (None or a
    problem string, e.g. a date line the renderer re-flowed) wins; if every engine renders but none validates, the
    first render is returned so the checks can report the problem. RenderError lists every failure."""
    errors, fallback = [], None
    for engine in order:
        try:
            pdf = render_with(engine, docx_bytes, work, title, google, docx_path)
        except RenderError as err:
            log.warning("%s PDF failed for %s: %s", engine, title, err)
            errors.append(str(err))
            continue
        problem = validate(pdf) if validate else None
        if not problem:
            return pdf, engine
        log.warning("%s PDF for %s rejected: %s; trying the next engine", engine, title, problem)
        errors.append(f"{engine}: {problem}")
        fallback = fallback or (pdf, engine)
    if fallback:
        return fallback
    raise RenderError("; ".join(errors) or "no PDF engine configured")
