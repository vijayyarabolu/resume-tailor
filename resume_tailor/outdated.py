"""a "ready" resume can still be stale. outdated(job, prof) lists why, in plain words, with no Claude call:

- made from your old resume: the base it was refilled from is no longer active (its file moved to archive/, or the
  base's content changed since: result.base.sha, recorded from on);
- old title rule: the title under the name is not the base's own title, or the summary does not open the way the
  rules now require;
- the stored PDF fails the free checks: more than one page, or a date that wraps ("date wraps: May / 2025").

The PDF/docx checks are cached per file (path, size, mtime), so the Ready list costs nothing after the first look.
times(job) gives "made" (the run that produced the current resume) and "updated" (a later re-import or Looks good).
"""
import logging
import re
from functools import lru_cache
from pathlib import Path

from . import checks, db, docxio, pdfx, rules

log = logging.getLogger(__name__)
OLD_BASE = "made from your old resume"
TITLE_RULE = "old title rule"
LATER_KINDS = ("reimport", "fix")               # rounds that edit an existing resume rather than make one


def _stat(path: str | None) -> tuple | None:
    try:
        st = Path(path).stat()
    except (OSError, TypeError):
        return None
    return (str(path), st.st_size, st.st_mtime)


def _short_date(message: str) -> str:
    """"the date 'Aug 2023 – May 2025' of Education wraps …" -> "date wraps: Aug 2023 – May 2025 (Education)"."""
    m = re.search(r"the date '([^']+)' of (.+?) wraps", message)
    return f"date wraps: {m.group(1)} ({m.group(2)})" if m else message


@lru_cache(maxsize=512)
def _file_problems(pdf_key: tuple, docx_key: tuple) -> tuple:
    """(problems, headline, summary) of one stored PDF + .docx (cached by path, size and mtime)."""
    pdf, docx = Path(pdf_key[0]), Path(docx_key[0])
    try:
        structure = docxio.structure(docxio.read(docx))
        pages = pdfx.info(pdf).pages
        by_page = [pdfx.flat_lines(pdfx.lines(pdf, p)) for p in range(1, pages + 1)]
    except (OSError, docxio.DocxError, pdfx.PdfError) as err:
        log.warning("re-check of %s failed: %s", pdf, err)
        return (), "", ""
    out = [f"{pages} pages"] if pages != 1 else []
    out += [_short_date(m) for m in checks.date_problems([], structure, by_page=by_page)]
    return tuple(out), structure.headline, structure.summary


def outdated(job: dict, prof) -> list:
    """Why this ready resume is stale (empty = current)."""
    if job.get("status") != "ready":
        return []
    result = db.result_of(job)
    files = result.get("files") or {}
    used = result.get("base") or {}
    base = prof.base(used.get("id") or job.get("base_id"))
    out = []
    if base is None or (used.get("sha") and used["sha"] != base.sha) or \
            (not used.get("sha") and files.get("base_pdf") and Path(files["base_pdf"]) != base.pdf):
        out.append(OLD_BASE)
    pdf_key, docx_key = _stat(files.get("pdf")), _stat(files.get("docx"))
    problems, headline, summary = _file_problems(pdf_key, docx_key) if pdf_key and docx_key else ((), "", "")
    if base is not None and OLD_BASE not in out:
        prules = rules.for_profile(prof.id, prof.rules)
        jrules = rules.for_job(prules, job.get("role"), base.structure.headline)
        # a resume made under the base-title rule is still current; no Outdated flood
        allowed = {base.structure.headline, rules.headline_of(jrules, base.structure.headline)}
        want = rules.headline_of(jrules, base.structure.headline)
        if not prules.get("headline_may_change", True) and headline and headline not in allowed:
            out.append(f"{TITLE_RULE}: headline '{headline}' (now '{want}')")
        years = prules.get("summary_years")
        starts = {f"{t} with {years}" for t in allowed} if years else {rules.summary_start(prules, base.id)} - {None}
        if starts and summary and not any(summary.startswith(s) for s in starts):
            start = rules.summary_start(jrules, base.id) or sorted(starts)[0]
            out.append(f"{TITLE_RULE}: summary opens '{' '.join(summary.split()[:5])}…' (now '{start}…')")
    elif OLD_BASE in out and headline and base is not None and headline != base.structure.headline:
        out.append(f"headline '{headline}'")
    return out + list(problems)


def badge(reasons: list) -> str | None:
    if not reasons:
        return None
    if OLD_BASE in reasons:
        return "Outdated: made from your old resume"
    if any(r.startswith(TITLE_RULE) for r in reasons):
        return "Outdated: old title rule"
    return "Outdated: " + reasons[0]


def times(job: dict) -> dict:
    """{'made': when the current resume was produced, 'updated': a later re-import / Looks good, or None,
    'queued', and's 'approved' (uploaded to Drive) / 'downloaded' (Download link), each None if never}."""
    result = db.result_of(job)
    records = [r for r in result.get("attempts") or [] if r.get("finished")]
    made_runs = [r["finished"] for r in records if r.get("kind") not in LATER_KINDS]
    later = [r["finished"] for r in records if r.get("kind") in LATER_KINDS]
    if (result.get("accepted") or {}).get("at"):
        later.append(result["accepted"]["at"])
    made = max(made_runs) if made_runs else job.get("finished_at")
    updated = max(later) if later else None
    return {"made": made, "updated": updated if updated and (not made or updated > made) else None,
            "queued": job.get("created_at"), "approved": (result.get("approved") or {}).get("at"),
            "downloaded": job.get("downloaded_at")}
