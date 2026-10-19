import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from resume_tailor import claude_runner as cr


class ClassifyTests(unittest.TestCase):
    def test_limit_messages(self):
        for text in ("You've hit your usage limit. Resets at 7pm (America/Denver)",
                     "Claude usage limit reached. Your limit will reset in 3 hours",
                     "rate limit exceeded; try again in 20 minutes"):
            self.assertEqual(cr.classify(text), "limit", text)

    def test_login_messages(self):
        for text in ("Not logged in. Please run /login", "Invalid API key · Please run /login",
                     "authentication_error: OAuth token has expired"):
            self.assertEqual(cr.classify(text), "login", text)

    def test_other_is_error(self):
        self.assertEqual(cr.classify("boom"), "error")
        self.assertEqual(cr.classify(""), "error")


class ResetTests(unittest.TestCase):
    def test_reset_in_hours_and_minutes(self):
        now = datetime(2026, 10, 6, 12, 0).astimezone()
        self.assertEqual(cr.parse_reset("will reset in 3 hours", now), now + timedelta(hours=3))
        self.assertEqual(cr.parse_reset("resets in 20 minutes", now), now + timedelta(minutes=20))

    def test_reset_at_clock_time_today_or_tomorrow(self):
        now = datetime(2026, 10, 6, 12, 0).astimezone()
        self.assertEqual(cr.parse_reset("Resets at 7pm (America/Denver)", now), now.replace(hour=19))
        self.assertEqual(cr.parse_reset("resets at 9:30am", now), (now + timedelta(days=1)).replace(hour=9, minute=30))

    def test_default_when_no_time(self):
        now = datetime(2026, 10, 6, 12, 0).astimezone()
        self.assertEqual(cr.parse_reset("usage limit", now, default_min=45), now + timedelta(minutes=45))


class ParsingTests(unittest.TestCase):
    def test_last_json_from_plain_and_streamed_output(self):
        self.assertEqual(cr._last_json('{"type": "result", "result": "x"}')["result"], "x")
        streamed = 'noise\n{"type": "system"}\n{"type": "result", "subtype": "success", "result": "ok"}\n'
        self.assertEqual(cr._last_json(streamed)["subtype"], "success")
        self.assertIsNone(cr._last_json(""))
        self.assertIsNone(cr._last_json("[1, 2]"))

    def test_embedded_json(self):
        self.assertEqual(cr._embedded_json('Here you go:\n```json\n{"a": 1}\n```')["a"], 1)
        self.assertIsNone(cr._embedded_json("no braces"))

    def test_usage_extraction(self):
        raw = {"usage": {"input_tokens": 2, "output_tokens": 30, "cache_read_input_tokens": 5,
                         "cache_creation_input_tokens": 10, "output_tokens_details": {"thinking_tokens": 7}}}
        self.assertEqual(cr._usage(raw), {"input_tokens": 2, "output_tokens": 30, "cache_read_input_tokens": 5,
                                          "cache_creation_input_tokens": 10, "thinking_tokens": 7})

    def test_env_strips_nested_session_and_sets_config_dir(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "/h"}):
            env = cr.env_for("~/.claude-sam")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].endswith("/.claude-sam"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", cr.env_for(None))

    def test_account_status_parses_claude_auth_status(self):
        payload = json.dumps({"loggedIn": True, "email": "p@example.com", "subscriptionType": "max",
                              "configDirectory": "/x/.claude-sam"})
        fake = mock.Mock(stdout=payload, returncode=0)
        with mock.patch("subprocess.run", return_value=fake):
            st = cr.account_status("~/.claude-sam")
        self.assertEqual((st["logged_in"], st["email"], st["subscription"]), (True, "p@example.com", "max"))
        with mock.patch("subprocess.run", side_effect=OSError("no claude")):
            self.assertFalse(cr.account_status(None)["logged_in"])

    def test_pause_text_uses_local_clock(self):
        when = datetime.now().astimezone().replace(hour=19, minute=5, second=0, microsecond=0)
        self.assertTrue(cr.pause_text(when).startswith("Paused until 7:05 PM"))


class RunTests(unittest.TestCase):
    def _run_with(self, stdout: str, returncode: int = 0, stderr: str = ""):
        fake = mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)
        with mock.patch("subprocess.run", return_value=fake) as run:
            res = cr.run("prompt", {"type": "object"}, model="sonnet", config_dir=None, timeout_s=5)
        return res, run

    def test_structured_output_success(self):
        payload = {"type": "result", "subtype": "success", "is_error": False, "result": "{}",
                   "structured_output": {"headline": "x"}, "usage": {"output_tokens": 3}, "total_cost_usd": 0.1,
                   "duration_ms": 1200, "num_turns": 1, "modelUsage": {"claude-sonnet-5-5": {}}}
        res, run = self._run_with(json.dumps(payload))
        self.assertTrue(res.ok)
        self.assertEqual(res.output, {"headline": "x"})
        self.assertEqual(res.model, "claude-sonnet-5-5")
        self.assertEqual(res.cost_usd, 0.1)
        cmd = run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertIn("--json-schema", cmd)

    def test_usage_limit_is_detected_with_reset_time(self):
        payload = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "You've hit your usage limit. Your limit will reset in 2 hours"}
        res, _ = self._run_with(json.dumps(payload), returncode=1)
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, "limit")
        self.assertIsNotNone(res.limit_until)
        self.assertGreater(res.limit_until, datetime.now().astimezone() + timedelta(minutes=110))

    def test_login_is_detected(self):
        res, _ = self._run_with("", returncode=1, stderr="Not logged in · Please run /login")
        self.assertEqual(res.kind, "login")

    def test_garbage_is_error(self):
        res, _ = self._run_with("I cannot do that", returncode=0)
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, "error")


if __name__ == "__main__":
    unittest.main()


class RealLimitMessageTests(unittest.TestCase):
    """The exact response seen on 2026-10-06: subtype 'success', is_error true, reset time without 'at'."""

    def test_hit_your_limit_is_a_limit_with_reset(self):
        text = "You've hit your limit · resets 12:10am (America/Denver)"
        self.assertEqual(cr.classify(text), "limit")
        now = datetime(2026, 10, 6, 23, 20).astimezone()
        self.assertEqual(cr.parse_reset(text, now), (now + timedelta(days=1)).replace(hour=0, minute=10))

    def test_success_subtype_with_is_error_limit(self):
        payload = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0,
                   "result": "You've hit your limit · resets 12:10am (America/Denver)"}
        fake = mock.Mock(stdout=json.dumps(payload), stderr="", returncode=0)
        with mock.patch("subprocess.run", return_value=fake):
            res = cr.run("p", {"type": "object"})
        self.assertEqual(res.kind, "limit")
        self.assertIsNotNone(res.limit_until)

    def test_plain_text_limit_answer_without_error_flag(self):
        payload = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                   "result": "You've hit your limit · resets 7pm (America/Denver)"}
        fake = mock.Mock(stdout=json.dumps(payload), stderr="", returncode=0)
        with mock.patch("subprocess.run", return_value=fake):
            res = cr.run("p", {"type": "object"})
        self.assertEqual(res.kind, "limit")
