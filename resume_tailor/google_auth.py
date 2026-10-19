"""Connect Google: OAuth 2.0 authorization code flow with PKCE, for a desktop app on loopback.

Google doesn't let an open-source app ship a shared client secret, so each person creates their own OAuth client
(type "Desktop app") in Google Cloud and pastes its ID and secret on the Settings page. For desktop clients Google
treats that secret as not confidential; PKCE is what protects the code exchange.

    start(redirect_uri)  -> the consent URL (state + code_challenge S256); the server redirects the browser there
    finish(state, code)  -> exchanges the code for tokens, stores the refresh token (0600), reads the account email
    disconnect()         -> revokes the token at Google and deletes it here
    status()             -> what the Settings page shows

The redirect URI is http://127.0.0.1:<port>/oauth/google/callback on the app's own port; Google accepts any
loopback port for desktop clients, so nothing else needs registering. Only the drive.file scope is asked for.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, gdocs

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = gdocs.NEEDED_SCOPE
STATE_TTL_S = 600
TIMEOUT_S = 30
_pending: dict = {}
_lock = threading.Lock()


class AuthError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def client() -> dict | None:
    """{client_id, client_secret} saved on the Settings page, or None."""
    try:
        data = json.loads(gdocs.client_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get("installed") or data.get("web") or data
    if entry.get("client_id") and entry.get("client_secret"):
        return {"client_id": entry["client_id"], "client_secret": entry["client_secret"]}
    return None


def parse_client(text: str = "", client_id: str = "", client_secret: str = "") -> dict:
    """The client from Google's downloaded JSON, or from the two fields typed in. ValueError when neither works."""
    if text and text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise ValueError(f"that is not the JSON file Google gave you: {err}") from None
        entry = data.get("installed") or data.get("web") or data
        client_id, client_secret = entry.get("client_id", ""), entry.get("client_secret", "")
        if data.get("web") and not data.get("installed"):
            raise ValueError('that is a "Web application" client; create one of type "Desktop app" (step 5)')
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("the client ID ends in .apps.googleusercontent.com; copy it from Google Cloud > Clients")
    if not client_secret:
        raise ValueError("the client secret is missing")
    return {"client_id": client_id, "client_secret": client_secret}


def save_client(entry: dict | None) -> None:
    """Store (or with None, forget) the OAuth client. Changing the client drops the old connection."""
    if entry is None:
        gdocs.client_path().unlink(missing_ok=True)
    else:
        config.write_private(gdocs.client_path(), json.dumps({"installed": entry}, indent=2) + "\n")
    _forget_tokens()


def _forget_tokens() -> None:
    gdocs.token_path().unlink(missing_ok=True)
    (config.DATA / "google_access.json").unlink(missing_ok=True)


def start(redirect_uri: str) -> str:
    """The Google consent URL for this attempt. Raises AuthError when no client is saved."""
    entry = client()
    if entry is None:
        raise AuthError("add your Google OAuth client ID and secret first (steps 1 to 6 on the Settings page)")
    verifier = _b64(secrets.token_bytes(48))
    state = _b64(secrets.token_bytes(24))
    with _lock:
        now = time.time()
        for key in [k for k, v in _pending.items() if now - v["at"] > STATE_TTL_S]:
            _pending.pop(key, None)
        _pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "at": now}
    params = {"client_id": entry["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
              "scope": SCOPE, "state": state, "access_type": "offline", "prompt": "consent",
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
              "include_granted_scopes": "true"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:300]
        raise AuthError(f"Google answered {err.code}: {detail}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise AuthError(f"can't reach Google: {err}") from err
    return json.loads(body or b"{}")


def finish(state: str, code: str, token_url: str = TOKEN_URL, google=None) -> dict:
    """Exchange the code (with the PKCE verifier) and store the refresh token. Returns {email}."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if pending is None or time.time() - pending["at"] > STATE_TTL_S:
        raise AuthError("this sign-in link expired or was already used; press Connect Google again")
    entry = client()
    if entry is None:
        raise AuthError("the Google OAuth client was removed during sign-in")
    answer = _post(token_url, {"code": code, "client_id": entry["client_id"], "client_secret": entry["client_secret"],
                               "redirect_uri": pending["redirect_uri"], "grant_type": "authorization_code",
                               "code_verifier": pending["verifier"]})
    if not answer.get("refresh_token"):
        raise AuthError("Google sent no refresh token; remove the app at myaccount.google.com/permissions and "
                        "connect again")
    if SCOPE not in (answer.get("scope") or ""):
        raise AuthError("Google Drive access was not granted (tick the Drive box on the consent screen)")
    token = {"refresh_token": answer["refresh_token"], "scope": answer.get("scope"), "connected_at": int(time.time())}
    config.write_private(gdocs.token_path(), json.dumps(token, indent=2) + "\n")
    if answer.get("access_token"):
        config.write_private(config.DATA / "google_access.json", json.dumps(
            {"access_token": answer["access_token"], "expires_at": time.time() + int(answer.get("expires_in", 3600))}))
    google = google or gdocs.Google()
    try:
        who = google.about()
    except gdocs.GoogleError:
        who = {}
    if who.get("email"):
        token["email"] = who["email"]
        config.write_private(gdocs.token_path(), json.dumps(token, indent=2) + "\n")
    return {"email": who.get("email")}


def disconnect(revoke_url: str = REVOKE_URL) -> dict:
    """Revoke at Google (best effort: offline still disconnects here) and delete the stored tokens."""
    revoked = False
    try:
        token = json.loads(gdocs.token_path().read_text())
    except (OSError, json.JSONDecodeError):
        token = {}
    if token.get("refresh_token"):
        try:
            _post(revoke_url, {"token": token["refresh_token"]})
            revoked = True
        except AuthError:
            revoked = False
    _forget_tokens()
    return {"revoked": revoked}


def status() -> dict:
    """{client, client_id, connected, email, connected_at} with nothing secret in it."""
    entry = client()
    try:
        token = json.loads(gdocs.token_path().read_text())
    except (OSError, json.JSONDecodeError):
        token = {}
    connected = bool(token.get("refresh_token")) and SCOPE in (token.get("scope") or "")
    cid = (entry or {}).get("client_id") or ""
    return {"client": entry is not None, "client_id": (cid[:12] + "..." + cid[-28:]) if len(cid) > 44 else cid or None,
            "connected": connected and entry is not None, "email": token.get("email"),
            "connected_at": token.get("connected_at")}


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/oauth/google/callback"


def pending_count() -> int:
    return len(_pending)

