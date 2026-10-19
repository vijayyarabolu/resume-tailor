"""A Google problem says what to do in plain words and never shows a file path or a developer command."""
import json
import tempfile
import unittest
from pathlib import Path

from resume_tailor import gdocs


class PlainGoogleErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.client = self.tmp / "google_client.json"
        self.client.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}))

    def error(self, token=None, client=None):
        path = self.tmp / "token.json"
        if token is not None:
            path.write_text(json.dumps(token))
        g = gdocs.Google(client_path=client or self.client, token_path=path, cache_path=self.tmp / "cache.json")
        with self.assertRaises(gdocs.GoogleError) as caught:
            g._refresh_token() if client is None else g._client()
        return str(caught.exception)

    def assert_plain(self, text):
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn("/", text.split("(")[0])                 # no path before the how-to
        self.assertNotIn("`", text)
        self.assertIn("Settings", text)

    def test_no_token_yet(self):
        text = self.error()
        self.assertTrue(text.startswith("Google is not connected"), text)
        self.assert_plain(text)

    def test_a_lapsed_sign_in(self):
        text = self.error({"lapsed": True})
        self.assertTrue(text.startswith("Google sign-in ran out"), text)
        self.assert_plain(text)

    def test_no_drive_access(self):
        text = self.error({"refresh_token": "r", "scope": "email"})
        self.assertTrue(text.startswith("Google sign-in does not include Drive"), text)
        self.assert_plain(text)

    def test_no_client_file(self):
        text = self.error(client=self.tmp / "missing.json")
        self.assertTrue(text.startswith("Google is not set up yet"), text)
        self.assert_plain(text)


if __name__ == "__main__":
    unittest.main()
