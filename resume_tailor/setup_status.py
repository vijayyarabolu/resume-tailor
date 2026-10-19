"""What the Settings page's status panel shows, and its Test buttons. Every row is one of:

    ok        connected and working (or tested fine)
    missing   not set up yet; the app still runs, this feature waits
    failing   set up, but the last test or use failed
    optional  not needed for what you use now
    demo      demo mode stands in for it

Nothing here returns a secret: keys and client IDs are masked."""
import os
import shutil
import threading
import time

from . import api_runner, claude_runner, config, docxio, google_auth, pdfx, profiles, render

_last: dict = {}                       # what -> {"ok", "detail", "at"}: the last Test result
_lock = threading.Lock()


def remember(what: str, result: dict) -> dict:
    with _lock:
        _last[what] = {**result, "at": int(time.time())}
    return result


def last(what: str) -> dict | None:
    return _last.get(what)


def _row(state: str, detail: str, **extra) -> dict:
    return {"state": state, "detail": detail, **extra}


def claude(settings: dict) -> dict:
    backend = settings.get("claude_backend") or "api"
    tested = last("claude")
    common = {"backend": backend, "model": settings.get("model"), "models": [list(m) for m in api_runner.MODELS],
              "key": api_runner.masked(config.anthropic_key()), "cli_found": bool(shutil.which(
                  settings.get("claude_bin") or "claude")), "tested": tested,
              "forced_demo": os.environ.get("RESUME_TAILOR_DEMO") == "1"}
    if backend == "demo":
        return _row("demo", "Demo mode: no AI is called; resumes keep your bullets and add matching skills",
                    **common)
    if backend == "cli":
        if not common["cli_found"]:
            return _row("missing", "Claude Code is not installed on this machine", **common)
        if tested and not tested.get("ok"):
            return _row("failing", tested.get("error") or "not logged in", **common)
        return _row("ok" if tested else "missing", tested.get("detail") if tested else
                    "Claude Code found; press Test to check its login", **common)
    if not common["key"]:
        return _row("missing", "No API key yet: resumes wait in the queue until you add one", **common)
    if tested and not tested.get("ok"):
        return _row("failing", tested.get("error") or "the key did not work", **common)
    return _row("ok", tested.get("detail") if tested else "Key saved; press Test to check it", **common)


def pdf(settings: dict) -> dict:
    from . import pipeline
    order = pipeline.engine_order(settings)
    google = google_auth.status()
    avail = {"libreoffice": config.soffice(settings), "word": render.word_available(), "google": google["connected"]}
    missing_tools = pdfx.tools_missing()
    tested = last("pdf")
    usable = [e for e in order if avail.get(e)]
    common = {"engine": settings.get("pdf_engine") or "auto", "order": list(order), "available": avail,
              "poppler_missing": missing_tools, "tested": tested}
    if missing_tools:
        return _row("missing", "poppler is missing (" + ", ".join(missing_tools) + "): install poppler-utils, "
                    "it reads the PDFs for the checks", **common)
    if not usable:
        return _row("missing", "No PDF engine: install LibreOffice (free) or connect Google", **common)
    if tested and not tested.get("ok"):
        return _row("failing", tested.get("error") or "the test render failed", **common)
    return _row("ok", tested.get("detail") if tested else f"Using {usable[0]}", **common)


def google(settings: dict, port: int | None = None) -> dict:
    st = google_auth.status()
    tested = last("google")
    common = {**st, "redirect_uri": google_auth.redirect_uri(port or int(settings.get("port") or 8770)),
              "tested": tested}
    if not st["client"]:
        return _row("optional", "Not set up. Needed only to save approved PDFs to Drive, edit in Google Docs, or use "
                    "Google as the PDF engine", **common)
    if not st["connected"]:
        return _row("missing", "OAuth client saved; press Connect Google", **common)
    if tested and not tested.get("ok"):
        return _row("failing", tested.get("error") or "Google refused the saved sign-in", **common)
    return _row("ok", f"Connected as {st.get('email') or 'your account'}", **common)


def people() -> dict:
    out = []
    for pid in sorted(p.name for p in config.PROFILES.iterdir() if p.is_dir()) if config.PROFILES.exists() else []:
        try:
            prof = profiles.load(pid)
            out.append({"id": pid, "name": prof.name, "bases": [b.label for b in prof.bases],
                        "facts": bool(prof.skill_text.strip()), "known_skills": len(prof.known_skills)})
        except Exception as err:            # a broken profile is shown with its problem, never a crash
            out.append({"id": pid, "name": pid, "bases": [], "error": str(err)[:300]})
    if not out:
        return _row("missing", "No profile yet: load the demo profile or create your own", list=out)
    broken = [p for p in out if p.get("error")]
    if broken:
        return _row("failing", f"{broken[0]['id']}: {broken[0]['error']}", list=out)
    return _row("ok", ", ".join(f"{p['name']} ({len(p['bases'])} base resume{'s' if len(p['bases']) != 1 else ''})"
                                 for p in out), list=out)


def _tilde(path) -> str:
    """The home folder with the user's own home shown as ~ (no user name on screen)."""
    from pathlib import Path
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def status(settings: dict, worker_running: bool, port: int | None = None) -> dict:
    return {"claude": claude(settings), "pdf": pdf(settings), "google": google(settings, port), "profiles": people(),
            "worker": _row("ok" if worker_running else "missing",
                           "Running" if worker_running else "Not running: start the app with `python -m resume_tailor "
                           "serve` (it runs the worker too)"),
            "home": _tilde(config.DATA), "tracker_url": settings.get("tracker_url")}


# ---------- the Test buttons ----------

def test_claude(settings: dict) -> dict:
    backend = settings.get("claude_backend") or "api"
    if backend == "demo":
        return remember("claude", {"ok": True, "detail": "Demo mode: nothing to test"})
    if backend == "cli":
        st = claude_runner.account_status(None, settings.get("claude_bin") or "claude")
        if st.get("logged_in"):
            return remember("claude", {"ok": True, "detail": f"Claude Code is logged in ({st.get('subscription') or st.get('method') or 'ok'})"})
        return remember("claude", {"ok": False, "error": st.get("error") or "Claude Code is not logged in: run "
                                                                             "`claude auth login`"})
    return remember("claude", api_runner.check(model=settings.get("model")))


def test_google(google) -> dict:
    from .gdocs import GoogleError
    try:
        who = google.about()
    except GoogleError as err:
        return remember("google", {"ok": False, "error": str(err)[:300]})
    return remember("google", {"ok": True, "detail": f"Drive answers for {who.get('email') or 'your account'}"})


def test_pdf(settings: dict, google, work) -> dict:
    """Render a real base resume (the first profile's, else the bundled example) with the engine in use."""
    from . import pipeline
    docx = None
    for pid in profiles.ids():
        try:
            docx = profiles.load(pid).bases[0].docx
            break
        except Exception:
            continue
    docx = docx or next((config.EXAMPLES / "alex-rivera" / "resumes").glob("*.docx"), None)
    if docx is None:
        return remember("pdf", {"ok": False, "error": "no .docx to test with"})
    work.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        data, engine = render.to_pdf(docx.read_bytes(), "pdf test", work, google, order=pipeline.engine_order(settings))
    except render.RenderError as err:
        return remember("pdf", {"ok": False, "error": str(err)[:300]})
    out = work / "pdf-test.pdf"
    out.write_bytes(data)
    try:
        pages = pdfx.info(out).pages
    except pdfx.PdfError as err:
        return remember("pdf", {"ok": False, "error": f"{engine} made a PDF, but it can't be read: {err}"})
    lines = len(docxio.structure(docxio.read(docx)).jobs)
    return remember("pdf", {"ok": True, "detail": f"{engine} rendered {docx.name} in {time.time() - started:.1f} s "
                                                  f"({pages} page{'s' if pages != 1 else ''}, {lines} roles read)"})
