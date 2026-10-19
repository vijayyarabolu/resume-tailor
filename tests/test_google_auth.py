"""Connect Google: the consent URL (PKCE S256, loopback redirect, drive.file only), the code exchange against a
local fake of Google's token endpoint, token storage (0600), refresh, and Disconnect. No network."""
import base64
import hashlib
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from resume_tailor import gdocs, google_auth
from tests.helpers import TempEnv

CLIENT = {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-test"}


class FakeGoogle(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        FakeGoogle.seen.append((self.path, form))
        if self.path == "/token" and form.get("grant_type") == "authorization_code":
            if form.get("code") != "good-code":
                return self._reply(400, {"error": "invalid_grant"})
            return self._reply(200, {"access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-1",
                                     "scope": google_auth.SCOPE, "token_type": "Bearer"})
        if self.path == "/token" and form.get("grant_type") == "refresh_token":
            return self._reply(200, {"access_token": "at-2", "expires_in": 3600})
        if self.path == "/revoke":
            return self._reply(200, {})
        self._reply(404, {})

    def do_GET(self):
        FakeGoogle.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/drive/v3/about"):
            return self._reply(200, {"user": {"emailAddress": "alex@example.com", "displayName": "Alex"}})
        self._reply(404, {})


class GoogleAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.env = TempEnv().__enter__()
        FakeGoogle.seen = []

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def google(self):
        return gdocs.Google(base_url=self.base)

    def test_client_from_fields_or_downloaded_json(self):
        self.assertEqual(google_auth.parse_client("", CLIENT["client_id"], CLIENT["client_secret"]), CLIENT)
        self.assertEqual(google_auth.parse_client(json.dumps({"installed": CLIENT})), CLIENT)
        with self.assertRaisesRegex(ValueError, "Desktop app"):
            google_auth.parse_client(json.dumps({"web": CLIENT}))
        with self.assertRaisesRegex(ValueError, "googleusercontent"):
            google_auth.parse_client("", "abc", "x")

    def test_start_needs_a_client(self):
        with self.assertRaises(google_auth.AuthError):
            google_auth.start(google_auth.redirect_uri(8770))

    def test_consent_url_uses_pkce_and_the_loopback_redirect(self):
        google_auth.save_client(CLIENT)
        self.assertEqual(gdocs.client_path().stat().st_mode & 0o777, 0o600)
        url = google_auth.start(google_auth.redirect_uri(8770))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertTrue(url.startswith(google_auth.AUTH_URL))
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8770/oauth/google/callback")
        self.assertEqual((q["scope"], q["code_challenge_method"], q["access_type"]),
                         ("https://www.googleapis.com/auth/drive.file", "S256", "offline"))
        verifier = google_auth._pending[q["state"]]["verifier"]
        want = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(q["code_challenge"], want)
        self.assertNotIn("client_secret", url)

    def test_finish_exchanges_the_code_with_the_verifier_and_stores_the_token(self):
        google_auth.save_client(CLIENT)
        url = google_auth.start(google_auth.redirect_uri(8770))
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        verifier = google_auth._pending[state]["verifier"]
        who = google_auth.finish(state, "good-code", token_url=f"{self.base}/token", google=self.google())
        self.assertEqual(who, {"email": "alex@example.com"})
        sent = next(form for path, form in FakeGoogle.seen if path == "/token")
        self.assertEqual((sent["code_verifier"], sent["redirect_uri"], sent["client_id"]),
                         (verifier, "http://127.0.0.1:8770/oauth/google/callback", CLIENT["client_id"]))
        self.assertEqual(gdocs.token_path().stat().st_mode & 0o777, 0o600)
        st = google_auth.status()
        self.assertEqual((st["connected"], st["email"]), (True, "alex@example.com"))
        self.assertNotIn("GOCSPX", json.dumps(st))
        with self.assertRaisesRegex(google_auth.AuthError, "expired or was already used"):
            google_auth.finish(state, "good-code", token_url=f"{self.base}/token", google=self.google())

    def test_a_bad_state_or_code_is_refused(self):
        google_auth.save_client(CLIENT)
        with self.assertRaises(google_auth.AuthError):
            google_auth.finish("made-up", "good-code", token_url=f"{self.base}/token")
        url = google_auth.start(google_auth.redirect_uri(8770))
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        with self.assertRaisesRegex(google_auth.AuthError, "400"):
            google_auth.finish(state, "bad-code", token_url=f"{self.base}/token")
        self.assertFalse(google_auth.status()["connected"])

    def test_refresh_then_disconnect_revokes_and_forgets(self):
        google_auth.save_client(CLIENT)
        gdocs.token_path().write_text(json.dumps({"refresh_token": "rt-1", "scope": google_auth.SCOPE}))
        g = self.google()
        self.assertEqual(g.access_token(), "at-2")                          # refreshed with the stored token
        self.assertEqual(g.about()["email"], "alex@example.com")
        done = google_auth.disconnect(revoke_url=f"{self.base}/revoke")
        self.assertTrue(done["revoked"])
        self.assertEqual(next(form for path, form in FakeGoogle.seen if path == "/revoke"), {"token": "rt-1"})
        self.assertFalse(gdocs.token_path().exists())
        self.assertFalse(google_auth.status()["connected"])

    def test_changing_the_client_drops_the_old_connection(self):
        google_auth.save_client(CLIENT)
        gdocs.token_path().write_text(json.dumps({"refresh_token": "rt-1", "scope": google_auth.SCOPE}))
        google_auth.save_client({**CLIENT, "client_id": "456-def.apps.googleusercontent.com"})
        self.assertFalse(gdocs.token_path().exists())


if __name__ == "__main__":
    unittest.main()
