"""The Anthropic API backend against a fake HTTP transport (no network, no key spent): the request it sends
(structured output with the schema, model, effort), the JSON it reads back, cost from usage, and how each kind of
error becomes ok / login / limit / error for the worker."""
import json
import unittest

from resume_tailor import api_runner, claude_runner, config, prompts
from tests.helpers import TempEnv

try:
    import httpx2 as httpx                            # the anthropic package (1.x) brings httpx2
except ImportError:
    httpx = None

KEY = "sk-ant-api03-" + "x" * 30


def _message(text: str, stop: str = "end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10000, "output_tokens": 5000, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}


@unittest.skipIf(httpx is None, "anthropic (and its httpx2) not installed")
class ApiRunnerTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.sent = []

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def client(self, status: int, body: dict, headers: dict | None = None):
        def handler(request):
            self.sent.append(request)
            return httpx.Response(status, json=body, headers=headers or {})
        return httpx.Client(transport=httpx.MockTransport(handler))

    def run_(self, status, body, headers=None, **kw):
        return api_runner.run("tailor this", prompts.REPAIR_SCHEMA, api_key=KEY, model="claude-sonnet-5-5",
                              http_client=self.client(status, body, headers), max_retries=0, **kw)

    def test_a_good_answer(self):
        out = {"bullets": [], "skills": [], "summary": None, "notes": ["ok"]}
        res = self.run_(200, _message(json.dumps(out)), effort="medium")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.output, out)
        self.assertEqual(res.cost_usd, 0.07)                       # 10k in at $2/M + 5k out at $10/M
        sent = json.loads(self.sent[0].content)
        self.assertEqual(sent["model"], "claude-sonnet-5-5")
        self.assertEqual(sent["output_config"]["effort"], "medium")
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        summary = sent["output_config"]["format"]["schema"]["properties"]["summary"]
        self.assertEqual(summary, {"anyOf": [{"type": "string"}, {"type": "null"}]})   # no type lists
        headers = self.sent[0].headers                             # anthropic 1.x: x-api-key; 2.x: a Bearer token
        self.assertIn(KEY, (headers.get("x-api-key"), headers.get("authorization", "").removeprefix("Bearer ")))

        self.assertNotIn("tool_choice", sent)

    def test_no_key_waits_for_one(self):
        res = api_runner.run("p", {}, api_key=None)
        self.assertEqual((res.ok, res.kind), (False, "login"))
        self.assertIn("Settings", res.error)

    def test_a_refused_key_is_login(self):
        res = self.run_(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        self.assertEqual(res.kind, "login")

    def test_no_credit_is_login_with_where_to_add_it(self):
        res = self.run_(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                         "message": "Your credit balance is too low to access the API"}})
        self.assertEqual(res.kind, "login")
        self.assertIn("platform.claude.com", res.error)

    def test_rate_limit_pauses_until_retry_after(self):
        res = self.run_(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
                        {"retry-after": "120"}, timeout_s=5)
        self.assertEqual(res.kind, "limit")
        self.assertIsNotNone(res.limit_until)

    def test_not_json_or_cut_off(self):
        res = self.run_(200, _message("not json"))
        self.assertEqual(res.kind, "error")
        res = self.run_(200, _message('{"bullets": [', stop="max_tokens"))
        self.assertIn("max_tokens", res.error)

    def test_a_refusal_is_an_error_not_a_crash(self):
        res = self.run_(200, _message("", stop="refusal"))
        self.assertEqual((res.ok, res.kind), (False, "error"))

    def test_check_counts_tokens_for_free(self):
        got = api_runner.check(api_key=KEY, model="claude-sonnet-5-5",
                               http_client=self.client(200, {"input_tokens": 8}), max_retries=0)
        self.assertTrue(got["ok"])
        self.assertTrue(self.sent[0].url.path.endswith("/v1/messages/count_tokens"))
        bad = api_runner.check(api_key=KEY, http_client=self.client(401, {"type": "error", "error": {
            "type": "authentication_error", "message": "nope"}}), max_retries=0)
        self.assertEqual(bad, {"ok": False, "error": "Anthropic refused this key"})


class BackendChoiceTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_the_runner_follows_the_setting(self):
        from resume_tailor import demo_runner
        self.assertIs(claude_runner.runner_for({"claude_backend": "demo"}), demo_runner.run)
        self.assertIs(claude_runner.runner_for({"claude_backend": "cli"}), claude_runner.run)
        self.assertTrue(callable(claude_runner.runner_for({"claude_backend": "api"})))

    def test_account_for_the_api_is_the_key(self):
        self.assertFalse(claude_runner.account(None, {"claude_backend": "api"})["logged_in"])
        config.save_secret("anthropic_api_key", KEY)
        self.assertTrue(claude_runner.account(None, {"claude_backend": "api"})["logged_in"])
        self.assertEqual(config.SECRETS_PATH.stat().st_mode & 0o777, 0o600)
        self.assertEqual(api_runner.masked(KEY), "sk-ant-...xxxx")
        self.assertTrue(claude_runner.account(None, {"claude_backend": "demo"})["logged_in"])

    def test_cost_estimates(self):
        self.assertEqual(api_runner.cost_usd("claude-haiku-5-5", {"input_tokens": 1_000_000}), 0.1)
        self.assertIsNone(api_runner.cost_usd("some-other-model", {"input_tokens": 5}))


if __name__ == "__main__":
    unittest.main()
