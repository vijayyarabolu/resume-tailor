"""a "ready" resume can still be stale. outdated(job, prof) lists why, in plain words, with no Claude call:

- made from your old resume: the base it was refilled from is no longer active (its file moved to archive/, or the
  base's content changed since: result.base.sha, recorded from on);
- old title rule: the title under the name is not the base's own title, or the summary does not open the way the
  rules now require;
- the stored PDF fails the free checks: more than one page, or a date that wraps ("date wraps: May / 2025").

The PDF/docx checks are cached per file (path, size, mtime), so the Ready list costs nothing after the first look.
times(job) gives "made" (the run that produced the current resume) and "updated" (a later re-import or Looks good).
"""
import json
import logging
import os
import re
import threading
from pathlib import Path

from . import checks, config, db, docxio, pdfx, rules

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


# the checks run pdftotext twice per resume; kept only in memory (and at most 512), every start of the server spent
# ~9 s on them for 243 ready resumes before a tracker's list could load. They now live in <home>/file_checks.json,
# keyed by path, size and mtime, so a start reads one small file.

DISK_NAME = "file_checks.json"
_KEPT: dict[str, list] = {}
_KEPT_FROM: list = [None]              # the data folder _KEPT was read from (tests switch it)
_KEPT_LOCK = threading.Lock()


def _disk() -> Path:
    return Path(config.DATA) / DISK_NAME


def _load_kept() -> None:
    if _KEPT_FROM[0] == str(config.DATA):
        return
    _KEPT.clear()
    try:
        kept = json.loads(_disk().read_text())
        _KEPT.update({k: v for k, v in kept.items() if all(_stat(f[0]) == tuple(f) for f in json.loads(k))})  # gone out
    except (OSError, ValueError, TypeError):
        pass                                        # none yet, or unreadable: worked out again as asked
    _KEPT_FROM[0] = str(config.DATA)


def _save_kept() -> None:
    path = _disk()
    part = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        part.write_text(json.dumps(_KEPT))
        part.replace(path)
    except OSError as err:
        log.warning("could not keep the file checks in %s: %s", path, err)


def _file_problems(pdf_key: tuple, docx_key: tuple) -> tuple:
    """(problems, headline, summary) of one stored PDF + .docx (kept by path, size and mtime, in memory and on disk)."""
    key = json.dumps([list(pdf_key), list(docx_key)])
    with _KEPT_LOCK:
        _load_kept()
        hit = _KEPT.get(key)
    if hit is not None:
        return tuple(hit[0]), hit[1], hit[2]
    out = _check_files(pdf_key, docx_key)
    if out is None:                     # a failed check (pdftotext missing, a file being written): asked again next time
        return (), "", ""
    with _KEPT_LOCK:
        _KEPT[key] = [list(out[0]), out[1], out[2]]
        _save_kept()
    return out


def _cache_clear() -> None:
    with _KEPT_LOCK:
        _KEPT.clear()
        _KEPT_FROM[0] = None


_file_problems.cache_clear = _cache_clear


def _check_files(pdf_key: tuple, docx_key: tuple) -> tuple | None:
    pdf, docx = Path(pdf_key[0]), Path(docx_key[0])
    try:
        structure = docxio.structure(docxio.read(docx))
        pages = pdfx.info(pdf).pages
        by_page = [pdfx.flat_lines(pdfx.lines(pdf, p)) for p in range(1, pages + 1)]
    except (OSError, docxio.DocxError, pdfx.PdfError) as err:
        log.warning("re-check of %s failed: %s", pdf, err)
        return None
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
