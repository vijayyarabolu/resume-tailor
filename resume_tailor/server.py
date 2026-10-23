"""Stdlib HTTP server on port 8770: the UI and the JSON API (docs/API.md). A token is required everywhere
(<home>/token, 0600): `?token=` on the index once sets an HttpOnly cookie, and API clients such as a job tracker send
`Authorization: Bearer <token>`. Answers only this machine unless settings `allow_lan` is on. Not meant to be
exposed to the internet."""
import base64
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from functools import lru_cache
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import (config, db, docedit, gdocs, google_auth, headline, jd, outdated, places, profiles, progress, purge,
               reasons, rules, setup_status, versions, worker)

log = logging.getLogger(__name__)
COOKIE = "rg_token"
STATIC = Path(__file__).resolve().parent / "static"
LAN_NETS = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"))
MAX_BODY = 8 * 1024 * 1024                  # a base resume upload is the largest body
MIN_JD_CHARS = 80
SSE_POLL_S = 0.5   # a new resume shows in a tracker within 2 s (one indexed read)
STATE_LIMIT, ARCHIVED_LIMIT = 300, 200      # /api/state for everyone (older callers)
STATE_PERSON_LIMIT = 5000                    # one person's page lists all of theirs, so its counts are right
SSE_KEEPALIVE_S = 15.0
_ASSET = re.compile(r"^[a-z0-9_-]+\.(css|js|html|png|svg|woff2)$")
_UNSAFE = re.compile(r'[\\/:*?"<>|\n\r\t]+')
FILE_KEYS = {"pdf": ("pdf", "application/pdf"), "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
             "png": ("png", "image/png"), "base.pdf": ("base_pdf", "application/pdf"), "base.png": ("base_png", "image/png")}


def token() -> str:
    env = os.environ.get("RESUME_TAILOR_TOKEN")
    if env:
        return env.strip()
    config.ensure_dirs()
    path = config.TOKEN_PATH
    if not path.exists() or not path.read_text().strip():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(24) + "\n")
    os.chmod(path, 0o600)
    return path.read_text().strip()


def client_allowed(ip: str, allow_lan: bool) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return client_allowed(str(addr.ipv4_mapped), allow_lan)
    return allow_lan and any(addr in net for net in LAN_NETS)


def _named_stem(prof, base, job: dict) -> str:
    """The base's own name with the title of the role it was tailored for (headline.file_stem)."""
    title = db.result_of(job).get("headline") if job.get("result") else None
    allowed = rules.for_profile(prof.id, prof.rules).get("headline_titles")      # rules.toml headline_titles
    title = title or headline.for_job(job.get("role") or "", base.label, allowed)
    return headline.file_stem(prof.name, base.docx.stem, title, [b.docx.stem for b in prof.bases])


def file_name_for(prof, job: dict) -> str:
    """Drive / Google Doc name = the person's resume name for this role + the company, e.g.
    Alex_Rivera_ML_Engineer_Resume(Acme).pdf, so the person can find it later. The title follows the role, not
    the base it was built from. Downloads use download_name (no company). The person's pattern only when the base
    is unknown."""
    base = prof.base(job.get("base_id")) if job.get("base_id") else None
    company = re.sub(r"\s+", " ", _UNSAFE.sub(" ", job["company"])).strip()
    if base is not None:
        stem = _named_stem(prof, base, job)
        return f"{stem}({company}).pdf" if company else stem + ".pdf"
    role = re.sub(r"[^A-Za-z0-9]+", "_", (db.result_of(job).get("headline") or job["role"])).strip("_")
    name = prof.name.replace(" ", "_")
    stem = prof.file_pattern.format(name=name, title=role, company=company)
    return stem if stem.lower().endswith(".pdf") else stem + ".pdf"


def download_name(prof, job: dict, ext: str) -> str:
    """Downloaded file = the person's real resume name for this role (Alex_Rivera_ML_Engineer_Resume.pdf), so
    recruiters see the same name as the person's other resumes."""
    base = prof.base(job.get("base_id")) if job.get("base_id") else None
    stem = _named_stem(prof, base, job) if base else file_name_for(prof, job)[:-4]
    return f"{stem}.{ext}"


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$")
EFFORTS = (None, "low", "medium", "high", "xhigh", "max")
INT_BOUNDS = {"concurrency": (1, 4), "max_attempts": (1, 6), "max_repairs": (0, 8), "max_claude_calls": (1, 10),
              "archived_delete_days": (0, 3650)}          # 0 = archived resumes are never deleted


def validate_settings(body: dict) -> dict:
    """Only known keys with safe values reach settings.json (model/effort become API or CLI arguments)."""
    out = {}
    if "model" in body:
        model = body["model"]
        model = None if model in (None, "", "default") else str(model)
        if model is not None and not _MODEL_RE.match(model):
            raise ValueError("model must be a plain model id such as claude-sonnet-5-5")
        out["model"] = model or config.DEFAULTS["model"]
    if "effort" in body:
        effort = body["effort"] or None
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {[e for e in EFFORTS if e]}")
        out["effort"] = effort
    if "pdf_engine" in body:
        if body["pdf_engine"] not in config.PDF_ENGINES:
            raise ValueError(f"pdf_engine must be one of {config.PDF_ENGINES}")
        out["pdf_engine"] = body["pdf_engine"]
    if "claude_backend" in body:
        if body["claude_backend"] not in config.CLAUDE_BACKENDS:
            raise ValueError(f"claude_backend must be one of {config.CLAUDE_BACKENDS}")
        out["claude_backend"] = body["claude_backend"]
    if "tracker_url" in body:
        url = str(body["tracker_url"] or "").strip() or None
        if url and not re.match(r"^https?://[^\s\"'<>]+$", url):
            raise ValueError("tracker_url must be an http(s) address")
        out["tracker_url"] = url
    if "default_profile" in body:
        pid = body["default_profile"] or None
        if pid is not None and pid not in profiles.ids():
            raise ValueError(f"no profile {pid!r}")
        out["default_profile"] = pid
    for key, (lo, hi) in INT_BOUNDS.items():
        if key in body:
            try:
                value = int(body[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an integer") from None
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            out[key] = value
    return out


def files_version(result: dict, job: dict) -> str:
    """when the files on screen were made; the UI and API clients add it to file URLs so no viewer keeps an old
    copy (a re-import or a note writes new files under the same /api/jobs/<id>/file/… address)."""
    finished = [a.get("finished") for a in result.get("attempts") or [] if a.get("finished")]
    return result.get("files_at") or (max(finished) if finished else None) or job.get("updated_at") or ""


@lru_cache(maxsize=1024)
def _sha_of(path: str, size: int, mtime: float) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pdf_sha(path: str | None) -> str | None:
    """sha256 of a stored PDF (cached by path, size and mtime); None when it is missing."""
    try:
        st = Path(path).stat()
    except (OSError, TypeError):
        return None
    return _sha_of(str(path), st.st_size, st.st_mtime)


def approved_stale(result: dict) -> bool:
    """The approved Drive PDF is not the version shown (a re-import or note came after; an Undo back to the approved
    version is not stale: the content is compared)."""
    approved = result.get("approved") or {}
    if not approved.get("at"):
        return False
    shown = (result.get("files") or {}).get("pdf")
    if approved.get("pdf") == shown:
        return False
    if approved.get("sha"):
        return approved["sha"] != pdf_sha(shown)
    if approved.get("pdf"):
        return True
    made = files_version(result, {})
    return bool(made) and made > approved["at"]


def _shown_as_doc(job: dict) -> bool:
    """The version on screen is the person's Doc as it is (it no longer fits the base layout)."""
    result = db.result_of(job)
    last = ((result.get("attempts") or [{}])[-1] or {}).get("kind")
    return last == "reimport" and bool((result.get("reimport") or {}).get("as_is"))


LEAN_KEYS = ("base", "approved", "dropped")       # the result fields a tracker's list needs
LEAN_LIMIT = 100_000


def _lean(view: dict) -> dict:
    return {**view, "result": {k: view["result"][k] for k in LEAN_KEYS if k in view["result"]}}


def _job_view(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k != "jd_text"}
    out["result"] = db.result_of(job)
    out["files_version"] = files_version(out["result"], job)
    out["undo_count"] = versions.count(job["id"])
    out["approved_stale"] = approved_stale(out["result"])
    out["what_changed"] = reasons.what_changed(out["result"])
    out["step"] = progress.get(job["id"])                      # a running Re-import's step, else None
    out["dropped_from"] = (out["result"].get("dropped") or {}).get("from") if job["status"] == "cancelled" else None
    if job["status"] == "needs_you":
        out["reasons"] = reasons.plain(out["result"], job.get("error"))
    if job["status"] == "needs_place":                          # the picker's cities + the person's own
        out["place_choices"], out["home"] = json.loads(job.get("place_choices") or "[]"), places.home(job["profile"])
    out["times"] = outdated.times(job)
    if job["status"] == "ready":
        try:
            stale = outdated.outdated(job, profiles.load(job["profile"]))
        except profiles.ProfileError:
            stale = []
        out["outdated"], out["badge"] = stale, outdated.badge(stale)
    return out


QUIET_POLLS = re.compile(r'"GET /api/(?:jobs\?[^ ]*lean=1|state)[^ ]* HTTP/[\d.]+" 2\d\d ')   # answered polls: no line
LEAN_KEEP_S = 60     # a kept view is rebuilt at least this often (Outdated badge, file times)
_LEAN: dict[int, tuple[tuple, float, dict]] = {}     # job id -> ((rev, step), built at, lean view)
_LEAN_LOCK = threading.Lock()
_LEAN_BUILDING: dict[str, threading.Lock] = {}       # one build per person at a time: a second asker waits, then reuses


def lean_jobs(conn, pid: str) -> list[dict]:
    """The lean list (every resume of the person, small result), newest first. A view is built again only when its
    row changed (rev), its re-import step moved, or it is older than LEAN_KEEP_S."""
    with _LEAN_LOCK:
        building = _LEAN_BUILDING.setdefault(pid, threading.Lock())
    with building:
        return _lean_build(conn, pid)


def _lean_build(conn, pid: str) -> list[dict]:
    at = time.monotonic()
    marks = [(jid, (rev, json.dumps(progress.get(jid)))) for jid, rev in db.revs(conn, pid, LEAN_LIMIT)]
    with _LEAN_LOCK:
        kept = {jid: _LEAN.get(jid) for jid, _ in marks}
    stale = [jid for jid, key in marks if not kept[jid] or kept[jid][0] != key or at - kept[jid][1] > LEAN_KEEP_S]
    built = {row["id"]: _lean(_job_view(row)) for row in db.jobs_without_jd(conn, stale)}
    listed = {jid for jid, _ in marks}
    with _LEAN_LOCK:
        for jid, key in marks:
            if jid in built:
                _LEAN[jid] = (key, at, built[jid])
        for jid in [j for j, kept in _LEAN.items() if kept[2].get("profile") == pid and j not in listed]:
            del _LEAN[jid]                                  # deleted, purged or past LEAN_LIMIT: not kept forever
        return [_LEAN[jid][2] for jid, _ in marks if jid in _LEAN]


class _ThreadConn:
    """The calling thread's connection, for helpers made once and used from every handler thread (gdocs.Google)."""

    def __init__(self, app: "App"):
        self._app = app

    def __getattr__(self, name):
        return getattr(self._app.conn, name)


class App:
    """Shared state for the handler threads."""

    def __init__(self, settings: dict | None = None, port: int | None = None):
        self.settings = settings or config.settings()
        self.port = port or int(self.settings.get("port") or 8770)
        self.token = token()
        self._local = threading.local()
        self._local.conn = db.connect()          # sets the schema up once; handler threads get their own
        self.lock = threading.Lock()             # one Google flow at a time
        self.google = gdocs.Google(_ThreadConn(self), self.settings)
        self.worker_thread = None

    @property
    def conn(self):
        """This thread's connection: statements on one shared connection from several threads raised
        'sqlite3.InterfaceError: bad parameter or other API misuse'."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = db.connect(setup=False)
        return conn

    def release(self) -> None:
        """A handler thread's connection is closed when its request is done (the start thread's stays)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and threading.current_thread() is not threading.main_thread():
            self._local.conn = None
            conn.close()

    def reload_settings(self) -> None:
        lan = self.settings.get("allow_lan")
        self.settings = {**config.settings(), "allow_lan": lan}
        self.google.settings = self.settings

    def with_google(self, pid: str, fn):
        """fn(google): one Google account per app, the one connected on the Settings page."""
        return fn(self.google)

    def worker_running(self) -> bool:
        return (self.worker_thread is not None and self.worker_thread.is_alive()) or worker.alive()

    def profile_summary(self) -> list:
        out = []
        for pid in profiles.ids():
            try:
                prof = profiles.load(pid)
            except Exception as err:
                out.append({"id": pid, "name": pid, "error": str(err), "bases": []})
                continue
            out.append({"id": pid, "name": prof.name, "bases": [{"id": b.id, "label": b.label} for b in prof.bases],
                        "pause": db.pause_of(self.conn, pid)})
        return out


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Handler(BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        line = fmt % args
        if QUIET_POLLS.search(line):                  # a tracker's polls were 5,500 lines a day
            return
        log.info("%s %s", self.address_string(), re.sub(r"token=[^&\s\"]+", "token=<redacted>", line))

    def finish(self):
        try:
            super().finish()
        finally:
            self.app.release()                   # this thread's own DB connection

    # ---------- helpers ----------

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data, headers: dict | None = None) -> None:
        self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length") from None
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def _given_token(self, query: dict, path: str) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        if COOKIE in jar:
            return jar[COOKIE].value
        return (query.get("token") or [None])[0] if path == "/" else None     # ?token= only on the index

    def _client_ok(self) -> bool:
        if client_allowed(self.client_address[0], bool(self.app.settings.get("allow_lan"))):
            return True
        self._error(403, "only this machine is allowed (settings allow_lan opens it to your LAN)")
        return False

    def _gate(self, query: dict, path: str = "") -> bool:
        if not self._client_ok():
            return False
        given = self._given_token(query, path)
        if not given or not secrets.compare_digest(given.encode(), self.app.token.encode()):
            self._error(401, "token required: open the link `python -m resume_tailor token` prints")
            return False
        return True

    # ---------- routing ----------

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):   # the asker hung up first (a tracker's timeout, a closed tab)

            log.info("GET %s: the asker went away before the answer", urlsplit(self.path).path)
        except Exception as err:                  # never drop the connection without an answer
            log.exception("GET %s failed", urlsplit(self.path).path)
            self._error(500, f"{type(err).__name__}: {err}"[:300])

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):   # the page was closed or reloaded: the work itself is done
            log.info("POST %s: the page went away before the answer", urlsplit(self.path).path)
        except Exception as err:
            log.exception("POST %s failed", urlsplit(self.path).path)
            self._error(500, f"{type(err).__name__}: {err}"[:300])

    def _get(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path
        if path.startswith("/static/"):
            return self._static(path[8:]) if self._client_ok() else None
        if path == "/oauth/google/callback":                   # Google's redirect: the state proves it, not a cookie
            return self._google_callback(query) if self._client_ok() else None
        if not self._gate(query, path):
            return
        if path == "/":
            return self._index(query)
        if path == "/oauth/google/start":
            return self._google_start()
        if path == "/api/setup":
            return self._json(200, {"ok": True, **setup_status.status(self.app.settings, self.app.worker_running(),
                                                                         self.app.port)})
        if path == "/api/token":
            return self._json(200, {"ok": True, "token": self.app.token})
        m = re.match(r"^/api/profiles/([a-z0-9_-]+)/files$", path)
        if m:
            return self._profile_files(m.group(1))
        if path == "/api/state":
            return self._state((query.get("profile") or [None])[0])
        if path == "/api/events":
            since = (query.get("since") or ["0"])[0]
            if since == "latest":                               # a tracker follows only what happens next

                return self._events(db.last_event_id(self.app.conn))
            return self._events(int(since) if since.isdigit() else 0)
        if path == "/api/jobs":
            pid = (query.get("profile") or [None])[0]
            if (query.get("lean") or [""])[0] == "1":            # a tracker reads every resume, small
                jobs = lean_jobs(self.app.conn, pid) if pid else [_lean(_job_view(j)) for j in
                                                                  db.list_jobs(self.app.conn, None, limit=LEAN_LIMIT)]
                return self._json(200, {"ok": True, "jobs": jobs})
            return self._json(200, {"ok": True, "jobs": [_job_view(j) for j in db.list_jobs(self.app.conn, pid)]})
        m = re.match(r"^/api/jobs/(\d+)$", path)
        if m:
            job = db.get_job(self.app.conn, int(m.group(1)))
            if job is None:
                return self._error(404, "no such job")
            return self._json(200, {"ok": True, "job": _job_view(job), "jd_text": job["jd_text"]})
        m = re.match(r"^/api/jobs/(\d+)/file/([a-z.]+)(/[^/]+)?$", path)
        if m:
            return self._file(int(m.group(1)), m.group(2), named=bool(m.group(3)),
                              download=(query.get("dl") or [""])[0] == "1")
        self._error(404, "not found")

    def _post(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if not self._gate(query, parts.path):
            return
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as err:
            return self._error(400, f"bad JSON: {err}")
        path = parts.path
        try:
            if path == "/api/jobs":
                return self._add_jobs(body)
            if path == "/api/jd/fetch":
                return self._fetch_jd(body)
            if path == "/api/jd/guess":
                company, role = jd.guess(str(body.get("text") or ""))
                return self._json(200, {"ok": True, "company": company, "role": role})
            if path == "/api/settings":
                return self._settings(body)
            if path.startswith("/api/setup/") or path.startswith("/api/profiles") or path == "/api/demo/install":
                return self._setup_post(path, body)
            if path == "/api/unarchive":
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
                    raise ValueError("ids: a list of job ids to restore")
                return self._json(200, {"ok": True, "restored": db.unarchive(self.app.conn, ids)})
            if path == "/api/undelete":                         # Undo of Delete for good / Clear archived
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
                    raise ValueError("ids: a list of job ids to bring back")
                return self._json(200, {"ok": True, "restored": db.undelete(self.app.conn, ids)})
            if path == "/api/archived/clear":                   # Clear archived (one person's)
                pid = str(body.get("profile") or "")
                if pid not in profiles.ids():
                    raise ValueError("profile: whose archived resumes to delete")
                ids = db.mark_delete(self.app.conn, db.archived_ids(self.app.conn, pid))
                return self._json(200, {"ok": True, "deleting": ids})
            if path == "/api/archive":
                ids = db.archive_many(self.app.conn, str(body.get("what") or ""), body.get("profile") or None)
                return self._json(200, {"ok": True, "archived": ids})
            m = re.match(r"^/api/jobs/(\d+)/(cancel|top|retry|approve|gdoc|reimport|regenerate|accept|fix|archive|undo_edit|delete|place)$",
                         path)
            if m:
                return self._action(int(m.group(1)), m.group(2), body)
        except gdocs.GoogleError as err:
            return self._error(502, f"Google: {err}")
        except (google_auth.AuthError, profiles.ProfileError) as err:
            return self._error(400, str(err))
        except (KeyError, ValueError) as err:
            return self._error(400, str(err))
        self._error(404, "not found")

    # ---------- pages & files ----------

    def _static(self, name: str) -> None:
        if not _ASSET.fullmatch(name) or not (STATIC / name).exists():
            return self._error(404, "no such asset")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype:
            ctype += "; charset=utf-8"
        self._send(200, (STATIC / name).read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _index(self, query: dict) -> None:
        headers = {}
        if query.get("token"):
            jar = cookies.SimpleCookie()
            jar[COOKIE] = self.app.token
            jar[COOKIE]["httponly"] = True
            jar[COOKIE]["samesite"] = "Lax"
            jar[COOKIE]["path"] = "/"
            jar[COOKIE]["max-age"] = str(365 * 24 * 3600)
            headers["Set-Cookie"] = jar[COOKIE].OutputString()
        self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8", headers)

    def _file(self, job_id: int, which: str, named: bool = False, download: bool = False) -> None:
        job = db.get_job(self.app.conn, job_id)
        if job is None or which not in FILE_KEYS:
            return self._error(404, "no such job or file")
        key, ctype = FILE_KEYS[which]
        path = (db.result_of(job).get("files") or {}).get(key)
        if not path or not Path(path).exists():
            return self._error(404, f"no {which} for job {job_id}")
        ext = which.split(".")[-1]
        name = download_name(profiles.load(job["profile"]), job, ext)
        if which in ("pdf", "docx") and not named:
            # Redirect so the URL ends in the real file name: Chrome's PDF viewer saves by URL, not by header.
            loc = quote(which) + "/" + quote(name)
            query = urlsplit(self.path).query
            return self._send(302, b"", "text/plain", {"Location": loc + ("?" + query if query else "")})
        ascii_name = name.encode("ascii", "ignore").decode() or f"resume.{ext}"
        if download:                                     # the Download links carry ?dl=1; the PDF view does not
            db.mark_downloaded(self.app.conn, job_id)
        self._send(200, Path(path).read_bytes(), ctype,
                   {"Content-Disposition": f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'})

    # ---------- api ----------

    def _state(self, pid: str | None = None) -> None:
        conn = self.app.conn
        keys = ("model", "effort", "concurrency", "pdf_engine", "max_attempts", "max_repairs", "default_profile",
                "archived_delete_days", "tracker_url", "claude_backend", "max_claude_calls")
        db.auto_archive(conn)
        people = self.app.profile_summary()
        viewer = None
        if pid == "auto":                                  # a first visit: the default person, else the first one
            pid = self.app.settings.get("default_profile") or (people[0]["id"] if people else None)
        pid = pid if any(p["id"] == pid for p in people) else None      # unknown person: everyone, as before
        self._json(200, {"ok": True, "profiles": people, "viewer": viewer, "profile": pid,
                         "jobs": [_job_view(j) for j in db.list_jobs(conn, pid, include_archived=False,
                                                                     limit=STATE_PERSON_LIMIT if pid else STATE_LIMIT)],
                         "archived": [_job_view(j) for j in db.archived(
                             conn, STATE_PERSON_LIMIT if pid else ARCHIVED_LIMIT, pid)],
                         "settings": {k: self.app.settings.get(k) for k in keys},
                         "last_event_id": db.last_event_id(conn), "now": db.now(),
                         "worker": self.app.worker_running(),
                         "setup": {"claude_ready": setup_status.claude(self.app.settings)["state"] in ("ok", "demo"),
                                   "demo": self.app.settings.get("claude_backend") == "demo"}})

    def _validate_job(self, item: dict, known: set) -> tuple:
        pid = str(item.get("profile") or "")
        if pid not in known:
            raise ValueError(f"unknown profile {pid!r}; choose from {sorted(known)}")
        jd_text = str(item.get("jd_text") or "").strip()
        if len(jd_text) < MIN_JD_CHARS:
            raise ValueError("jd_text is too short to be a job description")
        guessed = jd.guess(jd_text) if not (item.get("company") and item.get("role")) else (None, None)
        company = str(item.get("company") or "").strip() or guessed[0] or "Unknown"
        role = str(item.get("role") or "").strip() or guessed[1] or "Role"
        base = item.get("base_override") or None
        if base and profiles.load(pid).base(base) is None:
            raise ValueError(f"unknown base {base!r} for {pid}")
        jh_id = str(item["tracker_job_id"]) if item.get("tracker_job_id") else None
        workplace = item.get("workplace") or None                          # as the tracker resolved it
        if workplace not in (None, "remote", "hybrid", "onsite"):
            raise ValueError(f"workplace must be remote, hybrid or onsite, not {workplace!r}")
        location = str(item.get("location") or "").strip() or None
        status, choices, error = "queued", None, None
        if location and not places.LOCATION.match(location):        # never refused for its location
            location, choices = places.normalise(location)
            if location is None:
                status, error = "needs_place", places.NEEDS
        return (pid, company, role, jd_text, item.get("jd_url") or None, jh_id, base, item.get("note") or None,
                workplace, location, status, choices, error)

    def _add_jobs(self, body: dict) -> None:
        items = body.get("jobs") if isinstance(body.get("jobs"), list) else [body]
        known = set(profiles.ids())
        validated = [self._validate_job(item, known) for item in items]     # all or nothing
        ids = [db.add_job(self.app.conn, *fields) for fields in validated]
        self._json(201, {"ok": True, "ids": ids, "worker_awake": worker.wake()})

    def _fetch_jd(self, body: dict) -> None:
        try:
            found = jd.fetch(str(body.get("url") or ""))
        except jd.JdError as err:
            return self._json(200, {"ok": False, "error": str(err)})
        self._json(200, {"ok": True, **found})

    def _settings(self, body: dict) -> None:
        allowed = validate_settings(body)
        config.save_settings(allowed)
        self.app.reload_settings()
        worker.wake()
        self._json(200, {"ok": True, "settings": {k: self.app.settings.get(k) for k in allowed}})

    def _action(self, job_id: int, action: str, body: dict) -> None:
        conn = self.app.conn
        job = db.get_job(conn, job_id)
        if job is None:
            return self._error(404, "no such job")
        if action == "cancel":
            return self._json(200, {"ok": True, "status": db.cancel(conn, job_id)})
        if action == "top":                                     # Move to top
            db.move_to_top(conn, job_id)
            worker.wake()
            return self._json(200, {"ok": True, "status": job["status"]})
        if action in ("retry", "regenerate"):
            if job["status"] == "needs_place":                  # the city is picked first (action place)
                raise ValueError(f"job {job_id} needs its location picked first")
            if job["status"] in ("queued", "generating", "checking", "waiting"):
                raise ValueError(f"job {job_id} is {job['status']}; cancel it first")
            if body.get("current") and job.get("base_override") and \
                    profiles.load(job["profile"]).base(job["base_override"]) is None:
                db.update(conn, job_id, base_override=None)     # "with current resume": a retired pick is dropped
            db.requeue(conn, job_id, body.get("note") or None, body.get("base_override") or None)
            worker.wake()
            return self._json(200, {"ok": True, "status": "queued"})
        if action == "place":                                   # the person picked the contact line's city
            override, location = places.pick(body.get("location"))
            db.set_place(conn, job_id, override or job.get("workplace"), location)
            worker.wake()
            return self._json(200, {"ok": True, "status": "queued", "location": location})
        if action == "accept":
            return self._json(200, {"ok": True, "status": "ready", "despite": db.accept(conn, job_id)})
        if action == "fix":
            if _shown_as_doc(job):
                raise ValueError("this version is your Google Doc as it is (it no longer fits the base layout): "
                                 "fix it in the Doc and press Get my Doc edits, or Undo my edit")
            db.fix_with_note(conn, job_id, str(body.get("note") or ""))
            versions.snapshot(job, "fix", str(body.get("note") or "").strip()[:1000])   # for Undo
            worker.wake()
            return self._json(200, {"ok": True, "status": "queued"})
        if action == "undo_edit":
            done = versions.undo(conn, job)
            return self._json(200, {"ok": True, **done, "job": _job_view(db.get_job(conn, job_id))})
        if action == "delete":                                   # × Delete for good (archived only)
            ids = db.mark_delete(conn, [job_id])
            if not ids:
                raise ValueError("only an archived resume can be deleted for good: archive it first")
            return self._json(200, {"ok": True, "deleting": ids})
        if action == "archive":
            db.archive(conn, job_id)
            return self._json(200, {"ok": True, "archived": job_id})
        if action == "approve":
            return self._approve(job)
        if action == "gdoc":
            return self._gdoc(job)
        if action == "reimport":
            return self._reimport(job)
        self._error(404, "unknown action")

    def _approve(self, job: dict) -> None:
        conn, job_id = self.app.conn, job["id"]
        result = db.result_of(job)
        pdf = (result.get("files") or {}).get("pdf")
        if job["status"] != "ready" or not pdf:
            raise ValueError("only a ready resume can be approved")
        stale = approved_stale(result)
        if job.get("drive_file_id") and not stale and not self.headers.get("X-Resume-Tailor-Again"):
            return self._json(200, {"ok": True, "drive": result.get("approved") or {"file_id": job["drive_file_id"],
                                    "link": job.get("drive_link"), "name": job.get("drive_name")}, "already": True})
        if job.get("drive_file_id") and stale:                    # the edited version replaces it
            return self._approve_again(job, result, pdf)
        prof = profiles.load(job["profile"])
        name = file_name_for(prof, job)
        parts = tuple(self.app.settings.get("drive_root") or config.DEFAULTS["drive_root"]) + (prof.name.split()[0],)
        props = {"resume_tailor_job": str(job_id), "profile": job["profile"], "company": job["company"][:100],
                 "role": job["role"][:100]}
        with self.app.lock:
            made = self.app.with_google(job["profile"], lambda g: g.approve_upload(Path(pdf).read_bytes(), parts,
                                                                                   name, props))
        self._approved(job, result, pdf, made, "approved")

    def _approved(self, job: dict, result: dict, pdf: str, made: dict, verb: str) -> None:
        approved = {"file_id": made["id"], "link": made.get("webViewLink"), "name": made.get("name"), "at": db.now(),
                    "pdf": pdf, "sha": pdf_sha(pdf)}
        db.update(self.app.conn, job["id"], drive_file_id=made["id"], drive_link=made.get("webViewLink"),
                  drive_name=made.get("name"), result=json.dumps({**result, "approved": approved}))
        db.event(self.app.conn, job["id"], job["profile"], "note", f"{verb}: {made.get('name')}",
                 {"link": made.get("webViewLink")})
        self._json(200, {"ok": True, "drive": approved, **({"updated": True} if verb != "approved" else {})})

    def _approve_again(self, job: dict, result: dict, pdf: str) -> None:
        """The approved Drive file gets the edited PDF in place (same file, name and link, so any link a tracker
        keeps still works); a file gone from Drive is uploaded anew."""
        def update(g):
            try:
                return g.update_file(job["drive_file_id"], Path(pdf).read_bytes(), gdocs.PDF)
            except gdocs.GoogleError as err:
                if err.status != 404:
                    raise
                prof = profiles.load(job["profile"])
                parts = tuple(self.app.settings.get("drive_root") or config.DEFAULTS["drive_root"]) + (prof.name.split()[0],)
                return g.approve_upload(Path(pdf).read_bytes(), parts, file_name_for(prof, job),
                                        {"resume_tailor_job": str(job["id"]), "profile": job["profile"]})
        with self.app.lock:
            made = self.app.with_google(job["profile"], update)
        self._approved(job, result, pdf, made, "approved again (edited version)")

    def _gdoc(self, job: dict) -> None:
        conn, job_id = self.app.conn, job["id"]
        docx = (db.result_of(job).get("files") or {}).get("docx")
        if not docx:
            raise ValueError("no .docx for this job yet")
        prof = profiles.load(job["profile"])
        title = file_name_for(prof, job)[:-4]
        with self.app.lock:
            made = self.app.with_google(job["profile"], lambda g: g.make_doc(Path(docx).read_bytes(), title))
        db.update(conn, job_id, gdoc_id=made["id"], gdoc_link=made.get("webViewLink"))
        db.event(conn, job_id, job["profile"], "note", "opened as Google Doc", {"link": made.get("webViewLink")})
        self._json(200, {"ok": True, "link": made.get("webViewLink"), "id": made["id"]})

    def _reimport(self, job: dict) -> None:
        """the Doc's latest revision (fresh export), refilled and checked; never a Claude call."""
        conn, job_id = self.app.conn, job["id"]
        if job.get("archived_at"):
            raise ValueError("this resume is archived: Restore it first, then Get my Doc edits")
        if not job.get("gdoc_id"):
            raise ValueError("open it as a Google Doc first")
        shown = (db.result_of(job).get("files") or {}).get("docx")
        shown_bytes = Path(shown).read_bytes() if shown and Path(shown).exists() else None
        progress.step(job_id, "reading your Doc")
        try:
            with self.app.lock:
                docx_bytes, meta = self.app.with_google(
                    job["profile"], lambda g: docedit.export_latest(g, job["gdoc_id"], shown_bytes))
                doc_pdf = lambda: self.app.with_google(job["profile"], lambda g: g.export(job["gdoc_id"], gdocs.PDF))
                status = docedit.reimport(conn, job, docx_bytes, self.app.settings, self.app.google, meta, doc_pdf)
        finally:
            progress.clear(job_id)
        self._json(200, {"ok": True, "status": status, "job": _job_view(db.get_job(conn, job_id))})

    # ---------- setup: keys, Google, profiles ----------

    def _google_start(self) -> None:
        url = google_auth.start(google_auth.redirect_uri(self.app.port)) if google_auth.client() else None
        if url is None:
            return self._send(302, b"", "text/plain", {"Location": "/#settings"})
        self._send(302, b"", "text/plain", {"Location": url})

    def _google_callback(self, query: dict) -> None:
        state, code = (query.get("state") or [""])[0], (query.get("code") or [""])[0]
        error = (query.get("error") or [""])[0]
        try:
            if error:
                raise google_auth.AuthError("you cancelled the Google sign-in" if error == "access_denied"
                                            else f"Google said: {error}")
            who = google_auth.finish(state, code, google=self.app.google)
            setup_status.remember("google", {"ok": True, "detail": f"Connected as {who.get('email') or 'your account'}"})
            title, text = "Google connected", f"Connected as {who.get('email') or 'your Google account'}."
        except google_auth.AuthError as err:
            title, text = "Google not connected", str(err)
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>{title}</title><body style='font:16px system-ui;margin:3em auto;max-width:34em;padding:0 1em'>"
                f"<h1 style='font-size:1.3em'>{title}</h1><p>{_html(text)}</p>"
                f"<p>You can close this tab and go back to Resume Tailor's Settings page.</p>"
                f"<script>try{{window.opener&&window.opener.postMessage('google-auth','*')}}catch(e){{}}</script>")
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _profile_files(self, pid: str) -> None:
        folder = profiles.folder(pid)
        if not folder.is_dir():
            return self._error(404, f"no profile {pid}")
        resumes = [{"file": p.name, "size": p.stat().st_size, "pdf": p.with_suffix(".pdf").exists()}
                   for p in sorted((folder / "resumes").glob("*.docx"))]
        try:
            name = profiles.load(pid).name
            problem = None
        except Exception as err:
            name, problem = pid, str(err)
        self._json(200, {"ok": True, "id": pid, "name": name, "problem": problem, "resumes": resumes,
                         "files": {n: profiles.read_file(pid, n) for n in profiles.FILES}})

    def _setup_post(self, path: str, body: dict) -> None:
        app = self.app
        if path == "/api/setup/anthropic_key":
            key = str(body.get("key") or "").strip()
            if key and not re.match(r"^sk-ant-[A-Za-z0-9_\-]{20,}$", key):
                raise ValueError("that doesn't look like an Anthropic API key (they start with sk-ant-)")
            config.save_secret("anthropic_api_key", key or None)
            setup_status._last.pop("claude", None)
            worker.wake()
            return self._json(200, {"ok": True, "claude": setup_status.claude(app.settings)})
        if path == "/api/setup/google_client":
            if body.get("remove"):
                google_auth.save_client(None)
            else:
                google_auth.save_client(google_auth.parse_client(str(body.get("json") or ""),
                                                                 str(body.get("client_id") or ""),
                                                                 str(body.get("client_secret") or "")))
            setup_status._last.pop("google", None)
            return self._json(200, {"ok": True, "google": setup_status.google(app.settings, app.port)})
        if path == "/api/setup/google/disconnect":
            done = google_auth.disconnect()
            setup_status._last.pop("google", None)
            return self._json(200, {"ok": True, **done, "google": setup_status.google(app.settings, app.port)})
        if path == "/api/setup/test":
            what = body.get("what")
            if what == "claude":
                result = setup_status.test_claude(app.settings)
            elif what == "google":
                result = setup_status.test_google(app.google)
            elif what == "pdf":
                with app.lock:
                    result = setup_status.test_pdf(app.settings, app.google, config.DATA / "pdf-test")
            else:
                raise ValueError("what: claude, google or pdf")
            if what == "claude" and result.get("ok"):
                worker.wake()                                  # waiting jobs may run now
            return self._json(200, {"ok": True, "result": result})
        if path == "/api/demo/install":
            pid = profiles.install_example()
            if not config.anthropic_key() and app.settings.get("claude_backend") == "api":
                config.save_settings({"claude_backend": "demo"})
                app.reload_settings()
            worker.wake()
            return self._json(200, {"ok": True, "profile": pid, "claude_backend": app.settings.get("claude_backend")})
        if path == "/api/profiles":
            pid = profiles.create(str(body.get("id") or "").strip().lower(), str(body.get("name") or ""))
            return self._json(201, {"ok": True, "profile": pid})
        m = re.match(r"^/api/profiles/([a-z0-9_-]+)/(file|resume)$", path)
        if m and m.group(2) == "file":
            profiles.write_file(m.group(1), str(body.get("name") or ""), str(body.get("text") or ""))
            worker.wake()
            return self._json(200, {"ok": True})
        if m and m.group(2) == "resume":
            try:
                data = base64.b64decode(str(body.get("data") or ""), validate=True)
            except ValueError:
                raise ValueError("data: the .docx file, base64 encoded") from None
            saved = profiles.add_resume(m.group(1), str(body.get("filename") or "resume.docx"), data)
            worker.wake()
            return self._json(200, {"ok": True, "file": saved})
        self._error(404, "not found")

    def _events(self, since: int) -> None:
        """SSE: polls the events table once a second only while a browser is connected."""
        conn = db.connect()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last, last_sent = since, time.monotonic()
        try:
            while True:
                rows = db.events_since(conn, last)
                for row in rows:
                    last = row["id"]
                    self.wfile.write(f"id: {row['id']}\ndata: {json.dumps(row)}\n\n".encode())
                if rows:
                    self.wfile.flush()
                    last_sent = time.monotonic()
                elif time.monotonic() - last_sent > SSE_KEEPALIVE_S:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_sent = time.monotonic()
                time.sleep(SSE_POLL_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            conn.close()


def _warm() -> None:
    """Build every person's lean list once as the server starts (it reads each resume's files, which outdated.py
    caches), so the first Resumes look after a restart doesn't wait for it (cold, it took over 4 s, longer than a
    tracker's request timeout)."""
    conn = db.connect(setup=False)
    try:
        for pid in profiles.ids():
            lean_jobs(conn, pid)
    except Exception:                                    # a warm-up fault only means the first look waits
        log.exception("warming the ready resumes failed")
    finally:
        conn.close()


PURGE_EVERY_S = 5


def _purger(app: App) -> None:
    """resumes deleted for good go once their Undo grace is over; once a day, archived ones older than
    archived_delete_days (default 30, 0 = never), with their Google Doc copies."""
    conn, day = db.connect(setup=False), None

    def delete_doc(pid: str, doc: str) -> bool:
        return app.google.delete(doc)
    while True:
        try:
            purge.due(conn, delete_doc)
            today = db.now()[:10]
            if today != day:
                gone = purge.auto(conn, app.settings.get("archived_delete_days") or 0, delete_doc)
                if gone:
                    log.info("auto-clear: %s archived resume(s) older than %s days deleted", len(gone),
                             app.settings.get("archived_delete_days"))
                day = today
        except Exception:                                 # never stop: the next round tries again
            log.exception("purge round failed")
        time.sleep(PURGE_EVERY_S)


CHECKPOINT_S = 300


def _checkpoints() -> None:
    """The WAL copied back every few minutes (PASSIVE: never waits on a reader), so it stays small."""
    while True:
        time.sleep(CHECKPOINT_S)
        try:
            conn = db.connect(setup=False)
            try:
                db.checkpoint(conn)
            finally:
                conn.close()
        except sqlite3.Error as err:
            log.warning("WAL checkpoint skipped: %s", err)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """A client that hangs up between requests is one line, not a traceback (a tracker polling every few
        seconds does this many times a day)."""
        err = sys.exc_info()[1]
        if isinstance(err, (BrokenPipeError, ConnectionResetError)):
            log.debug("%s hung up: %s", client_address[0], type(err).__name__)       # a closed tab is not news

            return
        super().handle_error(request, client_address)


def make_server(app: App, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    Handler.app = app
    server = QuietServer((host, port), Handler)
    server.daemon_threads = True
    return server


def _run_worker(app: App) -> None:
    try:
        worker.Worker().main()
    except Exception:
        log.exception("the worker stopped; restart the app to start it again")


def serve(lan: bool = False, port: int | None = None, with_worker: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = config.settings()
    port = port or int(settings.get("port") or 8770)
    app = App(settings, port)
    if lan:
        app.settings = {**app.settings, "allow_lan": True}
    server = make_server(app, port, "0.0.0.0" if app.settings.get("allow_lan") else "127.0.0.1")
    threading.Thread(target=_purger, args=(app,), daemon=True, name="purge").start()
    threading.Thread(target=_warm, daemon=True, name="warm-ready").start()
    threading.Thread(target=_checkpoints, daemon=True, name="wal-checkpoint").start()
    if with_worker and not worker.wake():                  # no worker process yet: run one in this process
        app.worker_thread = threading.Thread(target=_run_worker, args=(app,), daemon=True, name="worker")
        app.worker_thread.start()
    log.info("Resume Tailor is running. Open this link (it carries your access token):")
    print(f"\n    http://127.0.0.1:{port}/?token={app.token}\n", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
