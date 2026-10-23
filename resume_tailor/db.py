"""SQLite queue. One row per tailoring job; events feed the SSE stream; pauses hold a person's queue after a
usage limit. WAL mode so the server and the worker read and write at the same time."""
import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

STATUSES = ("queued", "generating", "checking", "ready", "needs_you", "failed", "cancelled", "waiting",
            "needs_place")                  # waits for the person to pick the contact line's city
ACTIVE = ("generating", "checking")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile TEXT NOT NULL,
  company TEXT NOT NULL,
  role TEXT NOT NULL,
  jd_text TEXT NOT NULL,
  jd_url TEXT,
  tracker_job_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  base_id TEXT,                 -- the base resume used (chosen by score, or the override)
  base_override TEXT,           -- the user's pick, if any
  base_why TEXT,                -- one line: why this base
  note TEXT,                    -- instruction for a regenerate ("make it more MLOps-heavy")
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  elapsed_s REAL,
  error TEXT,                   -- plain reason for needs_you / failed / waiting
  result TEXT,                  -- JSON: attempts, checks, scores, files
  run_dir TEXT,
  model TEXT,
  account TEXT,                 -- email the Claude run used (never a token)
  drive_file_id TEXT,
  drive_link TEXT,
  drive_name TEXT,
  gdoc_id TEXT,
  gdoc_link TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(profile, status, id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER,
  profile TEXT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,           -- status | note | pause | resume | toast
  text TEXT NOT NULL,
  data TEXT                     -- JSON extras (status, attempt, ...)
);
CREATE TABLE IF NOT EXISTS pauses (
  profile TEXT PRIMARY KEY,
  until TEXT NOT NULL,
  reason TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gfolders (
  key TEXT PRIMARY KEY,
  file_id TEXT NOT NULL,
  link TEXT
);
"""


# columns added after the first schema (ALTER TABLE on open, so an existing data/resume_tailor.db keeps working)
MIGRATIONS = (("archived_at", "TEXT"),     # hidden from Queue / Ready, files kept, never deleted
              ("fix_note", "TEXT"),       # "Fix with note" -> one focused repair from the last resume
              ("restored_at", "TEXT"),    # Restore / Undo; auto-archive leaves a restored job alone
              ("workplace", "TEXT"),      # remote | hybrid | onsite, as the tracker or the JD says
              ("location", "TEXT"),       # the job's "City, ST" for the contact line (NULL = own city)
              ("priority", "INTEGER NOT NULL DEFAULT 0"),   # "Move to top" (higher runs first)
              ("downloaded_at", "TEXT"),  # last time a Download link fetched the PDF / .docx
              ("requeued_at", "TEXT"),    # a retry / regenerate is listed under the day it was queued again
              ("deleting_at", "TEXT"),    # Delete for good pressed; gone from every list, purged after the Undo grace
              ("place_choices", "TEXT"),  # JSON ["City, ST", ...] the person picks from (needs_place)
              ("rev", "INTEGER NOT NULL DEFAULT 0"))   # bumped on every UPDATE (jobs_rev trigger)
# The lean list (?lean=1, what a tracker polls) rebuilds a resume's view only when its row changed. Not every write
# sets updated_at, so a trigger counts every UPDATE, and the covering index lets the check skip the JD and result
# columns.
REV_SETUP = ("CREATE TRIGGER IF NOT EXISTS jobs_rev AFTER UPDATE ON jobs FOR EACH ROW WHEN NEW.rev = OLD.rev "
             "BEGIN UPDATE jobs SET rev = OLD.rev + 1 WHERE id = OLD.id; END;"
             "CREATE INDEX IF NOT EXISTS jobs_revs ON jobs(profile, deleting_at, id, rev);")
REVS_SQL = "SELECT id, rev FROM jobs WHERE profile = ? AND deleting_at IS NULL ORDER BY id DESC LIMIT ?"
LIVE = ("queued", "generating", "checking", "waiting")
FINISHED_EMPTY = ("cancelled", "failed")
AUTO_ARCHIVE_H = 24


def now_ms() -> str:
    """A UTC stamp with milliseconds: archive order must hold for two archives in the same second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# SQLite's locks (and a WAL's shared memory) do not cross a VM's shared folder or a network file system, so on Linux
# the app refuses a DB on these mounts (a DB opened through a VM's shared folder ended up malformed)
SHARED_MOUNTS = ("virtiofs", "9p", "fuse", "nfs", "nfs4", "cifs", "smbfs", "smb3", "vboxsf", "afpfs", "webdav",
                 "fakeowner", "prl_fs", "vmhgfs")
ALLOW_SHARED = "RESUME_TAILOR_ALLOW_SHARED_DB"


class SharedDatabase(RuntimeError):
    """The DB lives on a shared or network mount."""


def _mounts() -> str:
    try:
        with open("/proc/mounts") as f:
            return f.read()
    except OSError:
        return ""


def mount_type(path, mounts: str):
    """The file system type of the mount holding `path` (its longest mount point), from /proc/mounts text."""
    target, best, kind = str(path), -1, None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            point = parts[1].rstrip("/") or "/"
            if (point == "/" or target == point or target.startswith(point + "/")) and len(point) > best:
                best, kind = len(point), parts[2]
    return kind


def _ensure_local(path) -> None:
    if not sys.platform.startswith("linux") or os.environ.get(ALLOW_SHARED) == "1":
        return
    kind = mount_type(os.path.realpath(path), _mounts())
    if kind and (kind in SHARED_MOUNTS or kind.startswith("fuse.")):
        raise SharedDatabase(f"{path} is on a shared mount ({kind}), where SQLite's locks don't hold: set "
                             f"RESUME_TAILOR_HOME to a folder on a local disk ({ALLOW_SHARED}=1 opens it anyway)")



def connect(path=None, setup: bool = True) -> sqlite3.Connection:
    """setup=False: one more connection to a DB that is already set up (the server's per-thread ones)."""
    path = path or config.DB_PATH
    if str(path) != ":memory:":
        _ensure_local(path)
        config.ensure_dirs()
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None,
                           factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if not setup:
        return conn
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, kind in MIGRATIONS:
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
    conn.executescript(REV_SETUP)
    return conn


class Connection(sqlite3.Connection):
    """A connection threads may share: `tx()` holds its lock for the whole transaction, so two threads can never
    interleave BEGIN/COMMIT (and a loser's ROLLBACK can never undo the winner's writes)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()


@contextmanager
def tx(conn):
    lock = getattr(conn, "lock", None) or _FALLBACK_LOCK
    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


_FALLBACK_LOCK = threading.RLock()


# ---------- jobs ----------

def add_job(conn, profile: str, company: str, role: str, jd_text: str, jd_url: str | None = None,
            tracker_job_id: str | None = None, base_override: str | None = None, note: str | None = None,
            workplace: str | None = None, location: str | None = None, status: str = "queued",
            place_choices: list | None = None, error: str | None = None) -> int:
    """status "needs_place": it waits in Queue until the person picks the city (set_place)."""
    if status not in ("queued", "needs_place"):
        raise ValueError(f"a new job is queued or needs_place, not {status!r}")
    stamp = now()
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO jobs(profile, company, role, jd_text, jd_url, tracker_job_id, base_override, note, "
            "created_at, updated_at, workplace, location, status, place_choices, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile, company.strip(), role.strip(), jd_text, jd_url, tracker_job_id, base_override, note, stamp,
             stamp, workplace, location, status, json.dumps(place_choices) if place_choices is not None else None,
             error))
        job_id = cur.lastrowid
        _event(conn, job_id, profile, "status", f"{status.replace('_', ' ')}: {company} · {role}", {"status": status})
    return job_id


def set_place(conn, job_id: int, workplace: str | None, location: str | None) -> None:
    """the person picked the city of a needs_place job: it is queued with it. KeyError / ValueError."""
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "needs_place":
            raise ValueError(f"job {job_id} is {job['status']}; it has its location already")
        conn.execute("UPDATE jobs SET status = 'queued', workplace = ?, location = ?, error = NULL, updated_at = ? "
                     "WHERE id = ?", (workplace, location, now(), job_id))
        _event(conn, job_id, job["profile"], "status", f"queued: location {location or 'own city (remote)'}",
               {"status": "queued"})


def get_job(conn, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(conn, profile: str | None = None, statuses=None, limit: int = 200,
              include_archived: bool = True) -> list[dict]:
    where, params = ["deleting_at IS NULL"], []
    if not include_archived:
        where.append("archived_at IS NULL")
    if profile:
        where.append("profile = ?")
        params.append(profile)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*params, limit))]


def revs(conn, profile: str, limit: int) -> list[tuple[int, int]]:
    """(id, rev) of the person's listed jobs, newest first: what list_jobs would return, read from an index only."""
    return [(r[0], r[1]) for r in conn.execute(REVS_SQL, (profile, limit))]


def jobs_without_jd(conn, ids: list[int]) -> list[dict]:
    """The rows of these jobs with every column but jd_text (the lean list never shows the JD)."""
    cols = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(jobs)") if r["name"] != "jd_text")
    out = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out += [dict(r) for r in conn.execute(f"SELECT {cols} FROM jobs WHERE id IN ({','.join('?' * len(part))})", part)]
    return out


QUEUE_ORDER = "ORDER BY priority DESC, id"     # first in, first out; "Move to top" jumps ahead


def next_queued(conn, profile: str) -> dict | None:
    row = conn.execute(f"SELECT * FROM jobs WHERE profile = ? AND status = 'queued' {QUEUE_ORDER} LIMIT 1",
                       (profile,)).fetchone()
    return dict(row) if row else None


def queue_order(conn, profile: str) -> list[dict]:
    """The person's queued jobs in the order they will run."""
    return [dict(r) for r in conn.execute(f"SELECT * FROM jobs WHERE profile = ? AND status = 'queued' {QUEUE_ORDER}",
                                          (profile,))]


def move_to_top(conn, job_id: int) -> None:
    """A queued (or waiting) job runs next for its person; KeyError when unknown, ValueError when not waiting."""
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in ("queued", "waiting"):
            raise ValueError(f"job {job_id} is {job['status']}; only a queued job can move to the top")
        top = conn.execute("SELECT max(priority) FROM jobs WHERE profile = ?", (job["profile"],)).fetchone()[0] or 0
        conn.execute("UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ?", (top + 1, now(), job_id))
        _event(conn, job_id, job["profile"], "note", "moved to the top of the queue", None)


def claim(conn, job_id: int, run_dir: str) -> bool:
    """queued -> generating, once (the attempt counter is bumped by the pipeline)."""
    stamp = now()
    with tx(conn):
        cur = conn.execute("UPDATE jobs SET status = 'generating', started_at = COALESCE(started_at, ?), "
                           "updated_at = ?, run_dir = ?, error = NULL WHERE id = ? AND status = 'queued'",
                           (stamp, stamp, run_dir, job_id))
        if cur.rowcount:
            job = get_job(conn, job_id)
            _event(conn, job_id, job["profile"], "status", "generating", {"status": "generating"})
        return bool(cur.rowcount)


def set_status(conn, job_id: int, status: str, text: str | None = None, error: str | None = None,
               extra: dict | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    stamp = now()
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        fields = {"status": status, "updated_at": stamp, "error": error}
        if status in ("ready", "needs_you", "failed", "cancelled"):
            fields["finished_at"] = stamp
            if job.get("started_at"):
                fields["elapsed_s"] = (_parse(stamp) - _parse(job["started_at"])).total_seconds()
        fields.update(extra or {})
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
        _event(conn, job_id, job["profile"], "status", text or status, {"status": status, "error": error})


def update(conn, job_id: int, **fields) -> None:
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx(conn):
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


def requeue(conn, job_id: int, note: str | None = None, base_override: str | None = None, keep_attempts=False):
    """retry / regenerate: back to queued; a note or base override is kept for the next run."""
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] in ("generating", "checking") and not keep_attempts:
            raise ValueError(f"job {job_id} is running ({job['status']}): cancel it first")
        conn.execute("UPDATE jobs SET status = 'queued', note = COALESCE(?, note), base_override = COALESCE(?, "
                     "base_override), attempts = CASE WHEN ? THEN attempts ELSE 0 END, error = NULL, "
                     "finished_at = NULL, started_at = NULL, elapsed_s = NULL, cancel_requested = 0, archived_at = NULL, "
                     "fix_note = CASE WHEN ? THEN fix_note ELSE NULL END, "
                     # a person's retry / regenerate is a new resume on a new day; the worker's own requeue is not
                     "downloaded_at = CASE WHEN ? THEN downloaded_at ELSE NULL END, "
                     "requeued_at = CASE WHEN ? THEN requeued_at ELSE ? END, updated_at = ? WHERE id = ?",
                     (note, base_override, *[int(keep_attempts)] * 4, now(), now(), job_id))
        _event(conn, job_id, job["profile"], "status", "queued again" + (f": {note}" if note else ""),
               {"status": "queued"})


DROPPABLE = ("ready", "needs_you")


def cancel(conn, job_id: int) -> str:
    """A queued job is cancelled at once; a running one is asked to stop after its current step. A finished
    resume (ready / needs you) is dropped: cancelled, archived (restorable), never uploaded."""
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] in DROPPABLE:
            return _drop(conn, job)
        if job["status"] in ("queued", "waiting", "needs_place"):
            conn.execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
                         (now(), now(), job_id))
            _event(conn, job_id, job["profile"], "status", "cancelled", {"status": "cancelled"})
            return "cancelled"
        if job["status"] in ACTIVE:
            conn.execute("UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now(), job_id))
            _event(conn, job_id, job["profile"], "note", "cancel requested", None)
            return "cancel_requested"
        return job["status"]


def _drop(conn, job: dict) -> str:
    """Inside cancel()'s transaction: the resume leaves Queue / Ready / Needs you; a tracker sees "cancelled" (no
    resume); Restore (unarchive) puts back the status it had (result.dropped.from)."""
    stamp = now()
    result = {**result_of(job), "dropped": {"at": stamp, "from": job["status"], "error": job.get("error")}}
    conn.execute("UPDATE jobs SET status = 'cancelled', archived_at = ?, finished_at = ?, updated_at = ?, "
                 "result = ? WHERE id = ?", (now_ms(), stamp, stamp, json.dumps(result), job["id"]))
    _event(conn, job["id"], job["profile"], "status", f"cancelled by you (was {job['status'].replace('_', ' ')})",
           {"status": "cancelled", "archived": True})
    return "cancelled"


# ---------- review & clearing ----------

def accept(conn, job_id: int) -> list:
    """"Looks good": a needs-you resume becomes ready as it is; the failure reasons stay on record
    (result.accepted = {at, despite}). Returns the reasons it was accepted despite."""
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "needs_you":
            raise ValueError(f"job {job_id} is {job['status']}; only a needs-you resume can be accepted")
        result = result_of(job)
        if not (result.get("files") or {}).get("pdf"):
            raise ValueError(f"job {job_id} has no resume to accept; use Fix with note")
        despite = [f.get("message", "") for f in (result.get("checks") or {}).get("failures") or []] \
            or [job.get("error") or "unknown"]
        accepted = {"at": now(), "despite": despite}
        stamp = now()
        conn.execute("UPDATE jobs SET status = 'ready', error = NULL, result = ?, updated_at = ?, finished_at = ? "
                     "WHERE id = ?", (json.dumps({**result, "accepted": accepted}), stamp, stamp, job_id))
        _event(conn, job_id, job["profile"], "status", "accepted by user despite: " + "; ".join(despite)[:300],
               {"status": "ready", "accepted": True})
    return despite


def fix_with_note(conn, job_id: int, note: str) -> None:
    """"Fix with note": back to the queue with its rounds kept, so the pipeline resumes from the resume it
    produced and runs one repair focused on what the note names (pipeline._note_round)."""
    note = (note or "").strip()
    if not note:
        raise ValueError("write what to fix, e.g. \"only make the summary shorter\"")
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in DROPPABLE:
            raise ValueError(f"job {job_id} is {job['status']}; Fix with note is for a needs-you or ready resume")
        result = {**result_of(job), "fix_from": job["status"]}      # a note not applied keeps "ready"
        conn.execute("UPDATE jobs SET status = 'queued', fix_note = ?, error = NULL, finished_at = NULL, "
                     "started_at = NULL, elapsed_s = NULL, cancel_requested = 0, archived_at = NULL, updated_at = ?, "
                     "result = ? WHERE id = ?", (note[:1000], now(), json.dumps(result), job_id))
        _event(conn, job_id, job["profile"], "status", f"queued to fix: {note[:200]}", {"status": "queued"})


def clear_fix_note(conn, job_id: int) -> None:
    update(conn, job_id, fix_note=None)


def archive(conn, job_id: int) -> None:
    """Hide a finished job from the Queue / Ready lists (its files and row stay; a tracker still sees it)."""
    with tx(conn):
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] in LIVE or job["status"] in ("needs_you", "needs_place"):
            raise ValueError(f"job {job_id} is {job['status']}; only ready, cancelled or failed jobs are archived")
        conn.execute("UPDATE jobs SET archived_at = COALESCE(archived_at, ?), updated_at = ? WHERE id = ?",
                     (now_ms(), now(), job_id))
        _event(conn, job_id, job["profile"], "note", "archived", {"archived": True})


def archive_many(conn, which: str, profile: str | None = None) -> list:
    """'cancelled_failed' (Queue: Clear cancelled & failed) or 'approved' (Ready: Archive all approved).
    Returns the archived ids."""
    if which == "cancelled_failed":
        cond = f"status IN ({','.join('?' * len(FINISHED_EMPTY))})"
        params = list(FINISHED_EMPTY)
    elif which == "approved":
        cond, params = "status = 'ready' AND drive_file_id IS NOT NULL", []
    else:
        raise ValueError("archive what: 'cancelled_failed' or 'approved'")
    if profile:
        cond += " AND profile = ?"
        params.append(profile)
    with tx(conn):
        ids = [r["id"] for r in conn.execute(f"SELECT id FROM jobs WHERE archived_at IS NULL AND {cond}", params)]
        stamp = now_ms()
        label = {"cancelled_failed": "Clear cancelled & failed", "approved": "Archive all approved"}[which]
        for job_id in ids:
            conn.execute("UPDATE jobs SET archived_at = ?, updated_at = ? WHERE id = ?", (stamp, now(), job_id))
            _event(conn, job_id, profile, "note", f"archived ({label})", {"archived": True})
        if ids:
            _event(conn, None, profile, "note", f"archived {len(ids)} {which.replace('_', ' & ')} job(s)",
                   {"archived": ids})
    return ids


def mark_downloaded(conn, job_id: int) -> None:
    """the card says "downloaded 9:21 AM" (the latest download)."""
    with tx(conn):
        row = conn.execute("SELECT profile FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return
        conn.execute("UPDATE jobs SET downloaded_at = ? WHERE id = ?", (now(), job_id))
        _event(conn, job_id, row["profile"], "note", "downloaded", None)


def unarchive(conn, ids) -> list:
    """Restore / Undo: archived jobs come back to the list they left (their status, files and Drive link
    were never touched). Returns the ids restored; unknown or not-archived ids are skipped."""
    ids = [int(i) for i in ids]
    restored = []
    with tx(conn):
        for job_id in ids:
            row = conn.execute("SELECT * FROM jobs WHERE id = ? AND archived_at IS NOT NULL", (job_id,)).fetchone()
            if row is None:
                continue
            conn.execute("UPDATE jobs SET archived_at = NULL, restored_at = ?, updated_at = ? WHERE id = ?",
                         (now(), now(), job_id))
            _undrop(conn, dict(row))
            _event(conn, job_id, row["profile"], "note", "restored from the archive", {"archived": False})
            restored.append(job_id)
    return restored


def _undrop(conn, job: dict) -> None:
    """A resume dropped with Cancel comes back with the status it had."""
    result = result_of(job)
    dropped = result.get("dropped") or {}
    if job["status"] != "cancelled" or dropped.get("from") not in DROPPABLE:
        return
    rest = {k: v for k, v in result.items() if k != "dropped"}
    conn.execute("UPDATE jobs SET status = ?, error = ?, result = ? WHERE id = ?",
                 (dropped["from"], dropped.get("error"), json.dumps(rest), job["id"]))


def archived(conn, limit: int = 200, profile: str | None = None) -> list[dict]:
    """Every archived job (one person's when given), the most recently archived first (the Archived view)."""
    who, params = (" AND profile = ?", (profile,)) if profile else ("", ())
    return [dict(r) for r in conn.execute(f"SELECT * FROM jobs WHERE archived_at IS NOT NULL AND deleting_at IS NULL{who} "
                                          "ORDER BY archived_at DESC, id DESC LIMIT ?", (*params, limit))]


def auto_archive(conn, hours: float = AUTO_ARCHIVE_H) -> list:
    """Cancelled / failed jobs finished more than `hours` ago leave the Queue by themselves."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    marks = ",".join("?" * len(FINISHED_EMPTY))
    with tx(conn):
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM jobs WHERE archived_at IS NULL AND status IN ({marks}) AND "
            f"COALESCE(finished_at, updated_at) < ? AND restored_at IS NULL", (*FINISHED_EMPTY, cutoff))]
        for job_id in ids:
            conn.execute("UPDATE jobs SET archived_at = ? WHERE id = ?", (now_ms(), job_id))
    return ids


def cancel_requested(conn, job_id: int) -> bool:
    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def recover_stale(conn) -> int:
    """A worker that died mid-job leaves generating/checking rows: put them back in the queue at start."""
    with tx(conn):
        rows = conn.execute("SELECT id, profile FROM jobs WHERE status IN ('generating', 'checking')").fetchall()
        for r in rows:
            conn.execute("UPDATE jobs SET status = 'queued', updated_at = ? WHERE id = ?", (now(), r["id"]))
            _event(conn, r["id"], r["profile"], "status", "queued again (worker restarted)", {"status": "queued"})
    return len(rows)


# ---------- events ----------

def _event(conn, job_id, profile, kind, text, data) -> None:
    conn.execute("INSERT INTO events(job_id, profile, at, kind, text, data) VALUES (?,?,?,?,?,?)",
                 (job_id, profile, now(), kind, text, json.dumps(data) if data is not None else None))


def event(conn, job_id, profile, kind, text, data=None) -> None:
    with tx(conn):
        _event(conn, job_id, profile, kind, text, data)


def events_since(conn, last_id: int, limit: int = 200) -> list[dict]:
    rows = conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        out.append(d)
    return out


def last_event_id(conn) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
    return int(row["m"] or 0)


# ---------- pauses ----------

def set_pause(conn, profile: str, until: datetime, reason: str) -> None:
    stamp = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tx(conn):
        conn.execute("INSERT INTO pauses(profile, until, reason, at) VALUES (?,?,?,?) ON CONFLICT(profile) DO UPDATE "
                     "SET until = excluded.until, reason = excluded.reason, at = excluded.at",
                     (profile, stamp, reason, now()))
        _event(conn, None, profile, "pause", f"{profile}: paused until {stamp} ({reason})",
               {"until": stamp, "reason": reason})


def clear_pause(conn, profile: str) -> None:
    with tx(conn):
        if conn.execute("DELETE FROM pauses WHERE profile = ?", (profile,)).rowcount:
            _event(conn, None, profile, "resume", f"{profile}: resumed", None)


def pause_of(conn, profile: str) -> dict | None:
    row = conn.execute("SELECT * FROM pauses WHERE profile = ?", (profile,)).fetchone()
    if row is None:
        return None
    if _parse(row["until"]) <= datetime.now(timezone.utc):
        clear_pause(conn, profile)
        return None
    return dict(row)


def pauses(conn) -> dict[str, dict]:
    return {r["profile"]: dict(r) for r in conn.execute("SELECT * FROM pauses")}


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_ts(stamp: str) -> datetime:
    """A stored UTC stamp (YYYY-MM-DDTHH:MM:SSZ) as an aware datetime."""
    return _parse(stamp)


# ---------- drive folder cache ----------

def gfolder(conn, key: str) -> dict | None:
    row = conn.execute("SELECT file_id, link FROM gfolders WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def remember_gfolder(conn, key: str, file_id: str, link: str | None) -> None:
    with tx(conn):
        conn.execute("INSERT INTO gfolders(key, file_id, link) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                     "file_id = excluded.file_id, link = excluded.link", (key, file_id, link))


def result_of(job: dict) -> dict:
    try:
        return json.loads(job.get("result") or "{}")
    except json.JSONDecodeError:
        return {}


# ---------- Delete for good (archived resumes only) ----------

def mark_delete(conn, ids) -> list:
    """Archived resumes leave every list at once; purge.due removes them after the Undo grace."""
    stamp, done = now_ms(), []
    with tx(conn):
        for job_id in (int(i) for i in ids):
            cur = conn.execute("UPDATE jobs SET deleting_at = ? WHERE id = ? AND archived_at IS NOT NULL "
                               "AND deleting_at IS NULL", (stamp, job_id))
            if cur.rowcount:
                done.append(job_id)
    return done


def archived_ids(conn, profile: str) -> list:
    return [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE profile = ? AND archived_at IS NOT NULL "
                                          "AND deleting_at IS NULL ORDER BY id", (profile,))]


def undelete(conn, ids) -> list:
    done = []
    with tx(conn):
        for job_id in (int(i) for i in ids):
            if conn.execute("UPDATE jobs SET deleting_at = NULL WHERE id = ? AND deleting_at IS NOT NULL",
                            (job_id,)).rowcount:
                done.append(job_id)
    return done


def due_deletes(conn, grace_s: float) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE deleting_at IS NOT NULL AND deleting_at <= ?",
                                          (cutoff,))]


def old_archived(conn, days: int) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE archived_at IS NOT NULL AND archived_at < ? "
                                          "AND deleting_at IS NULL", (cutoff,))]


def purge_row(conn, job_id: int) -> None:
    with tx(conn):
        conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
