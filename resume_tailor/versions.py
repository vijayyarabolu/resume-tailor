""""Undo my edit": before a Re-import or a Fix with note replaces the resume a person sees, that version is
kept in data/runs/<id>/prev<N>/ (its .docx, PDF, page image + state.json with the status, error and result it had).
Undo puts the newest one back (status as it was then) and keeps the undone folder as undone<N>-<stamp>/, so it is
repeatable back to the first generated version and nothing is ever deleted. A fresh run (retry / regenerate) starts
a new history: pipeline moves these folders into its prev-<stamp>/ archive with the old attempts."""
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

PREV = re.compile(r"^prev(\d+)$")
UNDONE = re.compile(r"^undone\d+-")
FILE_KEYS = ("docx", "pdf", "png", "content")   # content: the JSON the version was built from (re-import, Undo)
KEEP_ON_UNDO = ("approved",)              # the Drive copy exists whatever version is shown


def _run_dir(job_id: int) -> Path:
    return config.RUNS / str(int(job_id))


def _prevs(job_id: int) -> list:
    """[(n, folder)] oldest first."""
    try:
        entries = list(_run_dir(job_id).iterdir())
    except OSError:
        return []
    found = [(int(m.group(1)), p) for p in entries if p.is_dir() and (m := PREV.match(p.name))]
    return sorted(found)


def count(job_id: int) -> int:
    """How many edits can be undone."""
    return len(_prevs(job_id))


def history_dirs(run_dir: Path) -> list:
    """prev<N>/ and undone<N>-…/ folders (a fresh run archives them with its old attempts)."""
    return [p for p in run_dir.iterdir() if p.is_dir() and (PREV.match(p.name) or UNDONE.match(p.name))]


def _with_paths(result: dict, folder: Path) -> dict:
    files = {**(result.get("files") or {})}
    for key in FILE_KEYS:
        if files.get(key):
            files[key] = str(folder / Path(files[key]).name)
    return {**result, "files": files}


def snapshot(job: dict, kind: str, tag: str = "") -> Path | None:
    """Keep the version shown now before `kind` ("reimport" / "fix") replaces it. None when there is no resume."""
    result = db.result_of(job)
    files = result.get("files") or {}
    if not files.get("pdf") or not Path(files["pdf"]).exists():
        return None
    prevs = _prevs(job["id"])
    folder = _run_dir(job["id"]) / f"prev{(prevs[-1][0] if prevs else 0) + 1}"
    folder.mkdir(parents=True)
    for key in FILE_KEYS:
        if files.get(key) and Path(files[key]).exists():
            shutil.copy2(files[key], folder / Path(files[key]).name)
    state = {"kind": kind, "tag": tag, "at": db.now(), "status": job["status"], "error": job.get("error"),
             "result": _with_paths(result, folder)}
    (folder / "state.json").write_text(json.dumps(state, indent=1, ensure_ascii=False))
    return folder


def discard_newest(job_id: int, kind: str, tag: str = "") -> None:
    """The edit that was just kept for did not produce a new version (a note not applied, a crashed re-import): set
    its copy aside (renamed undone<N>-…-unused, never deleted) so Undo does not offer the same version twice. Only
    the copy that edit made (same kind and tag) is touched."""
    prevs = _prevs(job_id)
    if not prevs:
        return
    n, folder = prevs[-1]
    try:
        state = json.loads((folder / "state.json").read_text())
    except (OSError, ValueError):
        return
    if state.get("kind") == kind and state.get("tag", "") == tag:
        folder.rename(folder.with_name(f"undone{n}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-unused"))


def undo(conn, job: dict) -> dict:
    """The newest kept version back on screen. Returns {status, kind, left}. ValueError when there is nothing to
    undo or the job is running."""
    if job["status"] in db.LIVE:
        raise ValueError(f"job {job['id']} is {job['status']}; undo after it finishes")
    prevs = _prevs(job["id"])
    if not prevs:
        raise ValueError("nothing to undo: this is the first version")
    n, folder = prevs[-1]
    state = json.loads((folder / "state.json").read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    kept = folder.with_name(f"undone{n}-{stamp}")
    folder.rename(kept)
    current = db.result_of(job)
    result = {**_with_paths(state["result"], kept), "files_at": db.now(),
              **{k: current[k] for k in KEEP_ON_UNDO if k in current}}
    status = state["status"] if state["status"] in ("ready", "needs_you") else "needs_you"
    db.update(conn, job["id"], result=json.dumps(result))
    db.set_status(conn, job["id"], status, f"undid the {_label(state['kind'])} (version {n} is back)",
                  error=state.get("error") if status == "needs_you" else None)
    return {"status": status, "kind": state["kind"], "left": count(job["id"])}


def _label(kind: str) -> str:
    return {"reimport": "Doc edits", "fix": "Fix with note"}.get(kind, kind)
