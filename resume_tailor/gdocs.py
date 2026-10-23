"""Google Drive over stdlib urllib, with the person's own OAuth client and refresh token (see google_auth.py for
the Connect Google flow). Scope drive.file: the app only ever sees files and folders it created itself, never the
rest of the person's Drive. Tokens live in <home>/google_token.json and short-lived access tokens in
<home>/google_access.json, both 0600.

Used for: docx -> Google Doc -> PDF export (the "google" PDF engine), "Open as Google Doc" + re-import (docx
export), and the approved PDF upload into <drive_root>/<first name>/ (never overwriting).
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)
# what the person can do, in plain words (the file paths go to the log, never on screen)
RECONNECT = "open Settings and press Connect Google"

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
FILE_FIELDS = "id,name,mimeType,webViewLink,md5Checksum,parents"
TIMEOUT_S = 60
EXPORT_TIMEOUT_S = 120
MAX_TRIES = 4
REFRESH_MARGIN_S = 90
RETRYABLE = (429, 500, 502, 503, 504)
NEEDED_SCOPE = "https://www.googleapis.com/auth/drive.file"


class GoogleError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def client_path() -> Path:
    return config.DATA / "google_client.json"


def token_path() -> Path:
    return config.DATA / "google_token.json"


class Google:
    def __init__(self, conn=None, settings: dict | None = None, client_path=None, token_path=None, cache_path=None,
                 base_url: str | None = None):
        self.conn = conn
        self.settings = settings or config.settings()
        self.client_path = client_path or globals()["client_path"]()
        self.token_path = token_path or globals()["token_path"]()
        self.cache_path = cache_path or (config.DATA / "google_access.json")
        self.base = base_url.rstrip("/") if base_url else None        # tests: a loopback fake
        self._access: dict = {}

    # ---------- auth ----------

    def _url(self, kind: str) -> str:
        if self.base:
            return {"token": f"{self.base}/token", "drive": f"{self.base}/drive/v3",
                    "upload": f"{self.base}/upload/drive/v3"}[kind]
        return {"token": TOKEN_URL, "drive": DRIVE, "upload": UPLOAD}[kind]

    def _client(self) -> dict:
        try:
            data = json.loads(self.client_path.read_text())
            entry = data.get("installed") or data.get("web") or data
        except (OSError, json.JSONDecodeError) as err:
            log.warning("Google client not readable at %s: %s", self.client_path, err)
            raise GoogleError("Google is not set up yet: open Settings, add your Google OAuth client and press "
                              "Connect Google") from err
        if not entry.get("client_id") or not entry.get("client_secret"):
            log.warning("%s is not a Desktop OAuth client", self.client_path)
            raise GoogleError("Google is not set up yet: the saved OAuth client has no client ID or secret "
                              "(open Settings)")
        return entry

    def _refresh_token(self) -> str:
        try:
            token = json.loads(self.token_path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            log.warning("Google token not readable at %s: %s", self.token_path, err)
            raise GoogleError(f"Google is not connected: {RECONNECT}") from err
        if token.get("lapsed") or not token.get("refresh_token"):
            raise GoogleError(f"Google sign-in ran out: {RECONNECT}")
        if NEEDED_SCOPE not in (token.get("scope") or ""):
            raise GoogleError(f"Google sign-in does not include Drive: {RECONNECT}")
        return token["refresh_token"]

    def _load_cache(self) -> dict:
        if self._access:
            return self._access
        try:
            self._access = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._access = {}
        return self._access

    def _save_cache(self, data: dict) -> None:
        self._access = data
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        tmp.replace(self.cache_path)

    def access_token(self, force: bool = False) -> str:
        cached = self._load_cache()
        if not force and cached.get("access_token") and cached.get("expires_at", 0) - time.time() > REFRESH_MARGIN_S:
            return cached["access_token"]
        entry = self._client()
        form = urllib.parse.urlencode({"client_id": entry["client_id"], "client_secret": entry["client_secret"],
                                       "refresh_token": self._refresh_token(),
                                       "grant_type": "refresh_token"}).encode()
        req = urllib.request.Request(self._url("token"), data=form, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                answer = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:300]
            if err.code in (400, 401) and "invalid_grant" in body:
                raise GoogleError(f"Google sign-in ran out or was revoked: {RECONNECT}", err.code) from err
            raise GoogleError(f"Google token refresh failed: {err.code} {body}", err.code) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise GoogleError(f"Google token refresh failed: {err}") from err
        if "access_token" not in answer:
            raise GoogleError(f"Google token refresh refused: {answer.get('error')}")
        self._save_cache({"access_token": answer["access_token"],
                          "expires_at": time.time() + int(answer.get("expires_in", 3600))})
        return answer["access_token"]

    # ---------- http ----------

    def call(self, method: str, url: str, params: dict | None = None, json_body=None, data: bytes | None = None,
             ctype: str | None = None, timeout: float = TIMEOUT_S) -> tuple:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        if json_body is not None:
            data, ctype = json.dumps(json_body).encode(), "application/json; charset=UTF-8"
        force, last = False, GoogleError("no attempt")
        for attempt in range(MAX_TRIES):
            headers = {"Authorization": f"Bearer {self.access_token(force=force)}"}
            force = False
            if ctype:
                headers["Content-Type"] = ctype
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                body = err.read().decode(errors="replace")
                last = GoogleError(f"{method} {url.split('?')[0]}: {err.code} {body[:300]}", err.code)
                if err.code == 401 and not force:
                    force = True
                    continue
                if err.code not in RETRYABLE:
                    raise last from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last = GoogleError(f"{method} {url.split('?')[0]}: {err}")
            time.sleep(min(8.0, 1.5 * (2 ** attempt)))
        raise last

    def get_json(self, url: str, params: dict | None = None) -> dict:
        return json.loads(self.call("GET", url, params)[1] or b"{}")

    def post_json(self, url: str, body: dict, params: dict | None = None) -> dict:
        return json.loads(self.call("POST", url, params, json_body=body)[1] or b"{}")

    # ---------- files ----------

    def _alive(self, file_id: str) -> bool:
        try:
            found = self.get_json(f"{self._url('drive')}/files/{file_id}", {"fields": "id,trashed"})
        except GoogleError as err:
            if err.status == 404:
                return False
            raise
        return not found.get("trashed")

    def _find_folder(self, name: str, parent: str) -> dict | None:
        q = f"name = '{_quote(name)}' and mimeType = '{FOLDER}' and '{parent}' in parents and trashed = false"
        found = self.get_json(f"{self._url('drive')}/files", {"q": q, "fields": "files(id,webViewLink)",
                                                                "spaces": "drive"})
        return (found.get("files") or [None])[0]

    def folder(self, parts) -> str:
        """My Drive/<parts...>, found or created level by level; ids cached in the gfolders table."""
        parent, record = "root", None
        for depth in range(1, len(parts) + 1):
            key = self._key("folder:" + "/".join(parts[:depth]))
            record = db.gfolder(self.conn, key) if self.conn is not None else None
            if record is None or not self._alive(record["file_id"]):
                made = self._find_folder(parts[depth - 1], parent) or self.post_json(
                    f"{self._url('drive')}/files",
                    {"name": parts[depth - 1], "mimeType": FOLDER, "parents": [parent]}, {"fields": "id,webViewLink"})
                record = {"file_id": made["id"], "link": made.get("webViewLink")}
                if self.conn is not None:
                    db.remember_gfolder(self.conn, key, made["id"], made.get("webViewLink"))
            parent = record["file_id"]
        return parent

    def _key(self, key: str) -> str:
        return key

    def move(self, file_id: str, parent: str) -> dict:
        """Into another folder (never deleted)."""
        now = self.get(file_id, "id,parents")
        return json.loads(self.call("PATCH", f"{self._url('drive')}/files/{file_id}",
                                    {"addParents": parent, "removeParents": ",".join(now.get("parents") or []),
                                     "fields": FILE_FIELDS}, json_body={})[1] or b"{}")

    def names_in(self, parent: str, prefix: str) -> list:
        q = f"'{parent}' in parents and trashed = false and name contains '{_quote(prefix)}'"
        found = self.get_json(f"{self._url('drive')}/files", {"q": q, "fields": "files(name)", "pageSize": "200",
                                                                "spaces": "drive"})
        return [f["name"] for f in found.get("files") or []]

    def unique_name(self, parent: str, name: str) -> str:
        """`name` if free, else `<stem>_v2.<ext>`, `_v3`, ... (a new upload never overwrites)."""
        taken = set(self.names_in(parent, name.rsplit(".", 1)[0]))
        if name not in taken:
            return name
        stem, dot, ext = name.rpartition(".")
        n = 2
        while f"{stem}_v{n}{dot}{ext}" in taken:
            n += 1
        return f"{stem}_v{n}{dot}{ext}"

    def upload(self, name: str, data: bytes, mime: str, parent: str, as_doc: bool = False,
               props: dict | None = None) -> dict:
        meta = {"name": name, "parents": [parent]}
        if as_doc:
            meta["mimeType"] = GDOC
        if props:
            meta["appProperties"] = {k: str(v) for k, v in props.items()}
        boundary = f"resume-tailor-{uuid.uuid4().hex}"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        status, raw = self.call("POST", f"{self._url('upload')}/files", {"uploadType": "multipart",
                                                                       "fields": FILE_FIELDS},
                                data=body, ctype=f"multipart/related; boundary={boundary}", timeout=EXPORT_TIMEOUT_S)
        return json.loads(raw or b"{}")

    def update_file(self, file_id: str, data: bytes, mime: str) -> dict:
        """New content for an existing file (an approved PDF replaced by its edited version)."""
        status, raw = self.call("PATCH", f"{self._url('upload')}/files/{file_id}", {"uploadType": "media",
                                                                                   "fields": FILE_FIELDS},
                                data=data, ctype=mime, timeout=EXPORT_TIMEOUT_S)
        return json.loads(raw or b"{}")

    def export(self, file_id: str, mime: str) -> bytes:
        return self.call("GET", f"{self._url('drive')}/files/{file_id}/export", {"mimeType": mime},
                         timeout=EXPORT_TIMEOUT_S)[1]

    def get(self, file_id: str, fields: str = FILE_FIELDS) -> dict:
        return self.get_json(f"{self._url('drive')}/files/{file_id}", {"fields": fields})

    def delete(self, file_id: str) -> bool:
        try:
            self.call("DELETE", f"{self._url('drive')}/files/{file_id}")
            return True
        except GoogleError:
            return False

    # ---------- the flows ----------

    def docx_to_pdf(self, docx_bytes: bytes, title: str) -> bytes:
        """Upload the .docx converted to a Google Doc in the tmp folder, export PDF, delete the temp Doc."""
        parent = self.folder(tuple(self.settings.get("drive_tmp") or ("Resume Tailor", "tmp")))
        made = self.upload(f"{title} {uuid.uuid4().hex[:8]}", docx_bytes, DOCX, parent, as_doc=True)
        if not made.get("id"):
            raise GoogleError(f"Drive did not return a file id for the converted Doc: {made}")
        try:
            pdf = self.export(made["id"], PDF)
        finally:
            self.delete(made["id"])
        if not pdf.startswith(b"%PDF-"):
            raise GoogleError("Drive export did not return a PDF")
        return pdf

    def docs_folder(self) -> str:
        return self.folder(tuple(self.settings.get("drive_docs") or ("Resume Tailor", "Docs")))

    def make_doc(self, docx_bytes: bytes, title: str) -> dict:
        """A Google Doc the person edits by hand (kept in the Docs folder)."""
        made = self.upload(title, docx_bytes, DOCX, self.docs_folder(), as_doc=True)
        if not made.get("id"):
            raise GoogleError(f"Drive did not return a file id: {made}")
        return made

    def about(self) -> dict:
        """{email, name} of the connected account (the Settings page's Test button)."""
        user = self.get_json(f"{self._url('drive')}/about", {"fields": "user(emailAddress,displayName)"}).get("user") or {}
        return {"email": user.get("emailAddress"), "name": user.get("displayName")}

    def approve_upload(self, pdf_bytes: bytes, folder_parts, name: str, props: dict | None = None) -> dict:
        parent = self.folder(tuple(folder_parts))
        final = self.unique_name(parent, name)
        made = self.upload(final, pdf_bytes, PDF, parent, props=props)
        if not made.get("id"):
            raise GoogleError(f"Drive did not return a file id for {final}: {made}")
        return made
