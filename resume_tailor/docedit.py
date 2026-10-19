"""'Open as Google Doc' -> the person edits -> Re-import.

The edited Doc's text (its .docx export, read fresh) is refilled into the base .docx so the formatting stays the
base's, the PDF is made and the 8 checks run. It is the person's own edit, so:
- it is always SHOWN (new files, cache-busted by `files_at`), even when a check fails: the failures are listed as
  problems and the person picks Looks good / Fix with note / Open as Google Doc / Undo / Cancel;
- nothing overwrites it: no Claude call, no title lock; their contact-line edit (e.g. the city) is carried over;
- what the base layout cannot carry (job titles, dates, Education, bold / spacing) is said, never dropped silently;
- the version it replaces is kept (versions.snapshot) for "Undo my edit";
- it moves to "needs you" only when the edit caused a NEW problem: problems the version before it already had under
  today's rules (e.g. a title rule added after it was made) keep a Ready resume in Ready, with a warning.
"""
import json
import logging
import re
import time
from dataclasses import replace
from pathlib import Path

from . import changes, config, db, docxio, gdocs, pdfx, pipeline, profiles, progress, render, versions
from .pipeline import Context, _attempt_record, _keep_notes, _now, _result_fields, _write

log = logging.getLogger(__name__)
FORMAT_NOTE = ("Formatting edits in the Doc (bold, spacing, fonts) are not kept: the text is put back into your "
               "base layout.")
NO_TEXT_CHANGE = "No text changes found in the Doc."
EXPORT_TRIES = 3              # Drive can serve the export from before the last edit for a moment
EXPORT_WAIT_S = 3.0
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")     # "fills 12% of the width" and "fills 9%" are the same problem


def _text(docx_bytes: bytes) -> str:
    return "\n".join(p.text for p in docxio.read(docx_bytes).paras)


def export_latest(google, doc_id: str, shown_docx: bytes | None = None, sleep=time.sleep) -> tuple:
    """(docx bytes, {modifiedTime, version}) of the Doc's latest revision: re-exported when the Doc changed while
    exporting, and (a moment later, up to EXPORT_TRIES) when the export still reads exactly like the version shown
    although Drive says the Doc was edited after it was made."""
    fields = "id,modifiedTime,createdTime,version"
    data, meta = b"", {}
    for attempt in range(EXPORT_TRIES):
        before = google.get(doc_id, fields)
        data = google.export(doc_id, gdocs.DOCX)
        meta = google.get(doc_id, fields)
        if meta.get("version") != before.get("version"):
            continue
        edited_later = (meta.get("modifiedTime") or "") > (meta.get("createdTime") or "")
        if shown_docx is None or not edited_later or _text(data) != _text(shown_docx):
            break
        if attempt + 1 < EXPORT_TRIES:
            sleep(EXPORT_WAIT_S)
    return data, {k: meta.get(k) for k in ("modifiedTime", "version")}


def _shown(result: dict, base) -> tuple:
    """(structure, content) of the version on screen now."""
    files = result.get("files") or {}
    try:
        struct = docxio.structure(docxio.read(files["docx"]))
    except (KeyError, OSError, docxio.DocxError):
        struct = base.structure
    content = None
    try:                                               # the version's own content, also after Undo
        content = json.loads(Path(files["content"]).read_text())
    except (KeyError, OSError, ValueError):
        content = None
    for record in reversed(result.get("attempts") or [] if content is None else []):
        path = (record.get("files") or {}).get("content")
        if path and (record.get("files") or {}).get("docx") == files.get("docx"):
            try:
                content = json.loads(Path(path).read_text())
            except (OSError, ValueError):
                content = None
            break
    return struct, content or struct.as_content()


def _merge_edits(old_edits: list, new_edits: list) -> list:
    """Contact edits stay relative to the base's text: a second edit of an edited segment replaces the first."""
    out = [list(e) for e in old_edits]
    for shown_text, wanted in new_edits:
        hit = next((e for e in out if e[1] == shown_text), None)
        if hit:
            out = [[e[0], wanted] if e is hit else e for e in out]
        else:
            out.append([shown_text, wanted])
    return [e for e in out if e[0] != e[1]]


def _carry_contact(ctx: Context, shown_struct, shown_content: dict, edited) -> tuple:
    """The person's contact-line edit as content keys (+ a problem when it cannot be placed)."""
    on_line = shown_content.get("user_location") or ctx.prules.get("location")
    user_location, edits, problem = changes.contact_change(shown_struct.contact, edited.contact, on_line)
    keys = {}
    if user_location or shown_content.get("user_location"):
        keys["user_location"] = user_location or shown_content["user_location"]
    merged = _merge_edits(shown_content.get("contact_edits") or [], edits)
    if merged:
        keys["contact_edits"] = merged
    return keys, problem


def _problem_key(failure) -> tuple:
    return failure.check, failure.name, _PERCENT.sub("#%", failure.message)


def _failing_before(ctx: Context, shown_content: dict, files: dict) -> set | None:
    """The problems the version on screen has under today's rules: its own .docx + PDF re-checked (no render, no
    Claude). None when it cannot be re-checked (then every problem of the edit counts as new, as before)."""
    if not files.get("docx") or not files.get("pdf"):
        return None
    if shown_content.get("user_location"):
        ctx = replace(ctx, job={**ctx.job, "location": shown_content["user_location"]})
    try:
        docx_path = Path(files["docx"])
        report = pipeline.check_files(ctx, docx_path, Path(files["pdf"]), docxio.structure(docxio.read(docx_path)))
    except Exception as err:                       # only a comparison: it must never stop the person's re-import
        log.warning("job %s: the version before the edit could not be re-checked: %s", ctx.job["id"], err)
        return None
    return {_problem_key(f) for f in report.failures}


def _place(previous: str, report, own: list, new: list) -> str:
    """ready when the edit passes; still ready when its only problems were there before the edit; else needs you."""
    if report is not None and not own and (report.ok or (previous == "ready" and not new)):
        return "ready"
    return "needs_you"


def _context(conn, job: dict, prof, base, result: dict, settings: dict, google) -> Context:
    engine = pipeline.engine_order(settings)[0]
    return Context(conn, job, prof, base, pipeline.measure_base(base, engine, google), docxio.read(base.docx),
                   settings, google, pipeline._score(prof, job, None), pipeline.base_fit(base, engine, google),
                   tuple(result.get("notes_confirmed") or ()))


def _as_is(ctx: Context, folder: Path, docx_bytes: bytes, doc_pdf, err: Exception) -> tuple:
    """The Doc no longer fits the base layout (a bullet or a skills line added / removed): show the person's Doc
    exactly as it is (Google's own PDF of it), with that as the problem. (files, failures)"""
    problem = f"your Doc no longer matches the base layout ({err}); shown as your Doc, not refilled into the base"
    if doc_pdf is None:
        raise err
    pdf_path, docx_path = folder / "resume.pdf", folder / "gdoc-export.docx"
    _write(pdf_path, doc_pdf())
    files = {"docx": str(docx_path), "pdf": str(pdf_path)}
    try:
        files["png"] = str(pdfx.png(pdf_path, folder / "page-1", ctx.settings.get("pdf_dpi", 110)))
    except pdfx.PdfError as perr:
        log.warning("job %s: page image of the Doc failed: %s", ctx.job["id"], perr)
    return files, [problem]


def reimport(conn, job: dict, docx_bytes: bytes, settings: dict | None = None, google=None,
             doc_meta: dict | None = None, doc_pdf=None) -> str:
    """Files go to data/runs/<id>/reimport<N>/. Returns the new status (ready / needs_you). `doc_pdf`: a callable
    giving Google's PDF of the Doc (used only when the Doc no longer fits the base layout)."""
    settings = settings or config.settings()
    job_id = job["id"]
    if job["status"] in db.LIVE:
        raise ValueError(f"Get my Doc edits works on a finished resume only (this one is {job['status']})")
    previous, elapsed = job["status"], job.get("elapsed_s")
    prof = profiles.load(job["profile"])
    result = db.result_of(job)
    base = prof.base((result.get("base") or {}).get("id") or job.get("base_id")) or prof.bases[0]
    run_dir = config.RUNS / str(job_id)
    n = 1 + sum(1 for p in run_dir.glob("reimport*") if p.is_dir())
    folder = run_dir / f"reimport{n}"
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / "gdoc-export.docx", docx_bytes)
    versions.snapshot(job, "reimport", folder.name)
    db.update(conn, job_id, status="checking")
    db.event(conn, job_id, prof.id, "status", f"Doc edits {n}: checking", {"status": "checking"})
    started = time.time()
    try:
        status = _rebuild(conn, job, prof, base, result, settings, google, docx_bytes, folder, n, started,
                          doc_meta, doc_pdf, previous)
    except Exception as err:                      # never leave the job in 'checking' (recover_stale would rerun it)
        log.exception("job %s: re-import %s crashed", job_id, n)
        versions.discard_newest(job_id, "reimport", folder.name)
        db.set_status(conn, job_id, previous, f"Doc edits {n} failed: {err}"[:200], error=str(err)[:900])
        raise
    finally:
        progress.clear(job_id)
    db.update(conn, job_id, elapsed_s=elapsed)          # the generation time stays the generation time
    return status


def _rebuild(conn, job, prof, base, result, settings, google, docx_bytes, folder, n, started, doc_meta,
             doc_pdf, previous) -> str:
    job_id = job["id"]
    edited = docxio.structure(docxio.read(docx_bytes))
    shown_struct, shown_content = _shown(result, base)
    ctx = _context(conn, job, prof, base, result, settings, google)
    before = _failing_before(ctx, shown_content, result.get("files") or {})
    contact, problem = _carry_contact(ctx, shown_struct, shown_content, edited)
    content = {**edited.as_content(), **contact}
    _write(folder / "content.json", content)
    own = [p for p in [problem] + changes.not_kept(shown_struct, edited) if p]
    try:
        progress.step(job_id, "building the PDF")
        built = pipeline.build_and_check(ctx, content, folder, {"content": str(folder / "content.json")}, lock=False,
                                         on_step=lambda text: progress.step(job_id, text))
        files, failures, report = built.files, built.failures, built.report
        fields = _keep_notes(_result_fields(ctx, built, content, n, result.get("model")), result)
    except (docxio.DocxError, render.RenderError) as err:
        files, failures = _as_is(ctx, folder, docx_bytes, doc_pdf, err)
        report, fields = None, {"final_attempt": n, "checks": {"ok": False, "failures": [
            {"check": 2, "name": "layout", "message": failures[0]}]}}
    old = [f.message for f in report.failures if _problem_key(f) in before] if report is not None and before else []
    new = [m for m in failures if m not in old]
    status = _place(previous, report, own, new)
    said = changes.describe(shown_content, content)
    result.setdefault("attempts", []).append(_attempt_record(n, started, None, failures, None, files, "reimport"))
    result.update(fields)
    result.setdefault("files", {}).update({k: files[k] for k in versions.FILE_KEYS if k in files})
    if report is None:                                 # shown as the Doc: no content in the base layout to edit
        result["files"].pop("content", None)
    result["files_at"] = _now()
    result["reimport"] = {"n": n, "at": _now(), "doc": doc_meta or {}, "changes": said or [NO_TEXT_CHANGE],
                          "not_kept": own, "format_note": FORMAT_NOTE, "as_is": report is None,
                          "old_problems": old, "new_problems": own + new,
                          "moved": {"from": previous, "to": status} if status != previous else None,
                          "steps": progress.timings(job_id)}
    db.update(conn, job_id, result=json.dumps(result))
    if status == "ready":
        text = (f"Doc edits {n}: stays ready; {len(old)} problem(s) were already there before your edit" if old
                else f"ready after Doc edits {n}")
        db.set_status(conn, job_id, "ready", text)
        return "ready"
    reason = "; ".join(own + new + old)[:900]                 # the edit's own problems first
    db.set_status(conn, job_id, "needs_you", f"Doc edits {n}: your edit is shown; {len(own + new)} new problem(s)",
                  error=reason)
    return "needs_you"
