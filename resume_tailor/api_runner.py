"""Claude through the Anthropic API with your own key (the default backend). One Messages call per round, with
structured output so the answer is always the JSON object the schema describes.

Anthropic has no sign-in for third-party apps, so this needs an API key from platform.claude.com. The key lives in
<home>/secrets.json (0600) or ANTHROPIC_API_KEY; it is never logged or shown in full.

Errors become the same RunResult kinds the CLI runner uses: "login" (key missing, refused or out of credit: jobs
wait until it is fixed), "limit" (rate limited or overloaded: the queue pauses until the retry time) and "error"."""
import json
import logging
from datetime import datetime, timedelta

from . import config
from .claude_runner import RunResult, parse_reset

log = logging.getLogger(__name__)
# USD per million tokens (input, output, cache read), from https://platform.claude.com/docs/en/about-claude/pricing
# (checked October 2026). Unknown models get no cost estimate.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
MODELS = (("claude-sonnet-5-5", "Claude Sonnet 5.5 (recommended)"), ("claude-opus-5-5", "Claude Opus 5.5"),
          ("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)"))
OFFLINE_PAUSE_MIN = 5


def api_schema(schema):
    """The schema in the form structured outputs accepts: a type list such as ["string", "null"] becomes anyOf."""
    if isinstance(schema, dict):
        out = {k: api_schema(v) for k, v in schema.items()}
        kind = out.get("type")
        if isinstance(kind, list):
            out.pop("type")
            out["anyOf"] = [{"type": k} for k in kind]
        return out
    if isinstance(schema, list):
        return [api_schema(v) for v in schema]
    return schema


def cost_usd(model: str | None, usage: dict) -> float | None:
    price = PRICES.get(model or "")
    if price is None:
        return None
    fresh = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    total = (fresh * price[0] + (usage.get("output_tokens") or 0) * price[1]
             + (usage.get("cache_read_input_tokens") or 0) * price[2]) / 1_000_000
    return round(total, 5)


def client(api_key: str | None = None, timeout_s: float = 900, http_client=None, max_retries: int = 2):
    import anthropic                          # imported here so demo mode and the tests run without the package
    key = api_key or config.anthropic_key()
    kwargs = {"api_key": key, "timeout": float(timeout_s), "max_retries": max_retries}
    if http_client is not None:
        kwargs["http_client"] = http_client
    return anthropic.Anthropic(**kwargs)


def _usage(msg) -> dict:
    u = getattr(msg, "usage", None)
    if u is None:
        return {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: getattr(u, k) for k in keys if getattr(u, k, None) is not None}


def _retry_until(err, default_min: float) -> datetime:
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    try:
        seconds = float(headers.get("retry-after"))
        return datetime.now().astimezone() + timedelta(seconds=max(1.0, seconds))
    except (TypeError, ValueError):
        return parse_reset(str(err), default_min=int(default_min))


def run(prompt: str, schema: dict, *, model: str | None = None, effort: str | None = None,
        timeout_s: int = 900, default_pause_min: int = 30, api_key: str | None = None, max_tokens: int = 16000,
        http_client=None, max_retries: int = 2, **_ignored) -> RunResult:
    """One call (the SDK itself retries a 429 or 5xx `max_retries` times first). `http_client` (tests) replaces the
    network."""
    try:
        import anthropic
    except ImportError:
        return RunResult(False, error="the anthropic package is not installed: pip install anthropic", kind="error")
    key = api_key or config.anthropic_key()
    if not key:
        return RunResult(False, error="no Anthropic API key: add one on the Settings page", kind="login")
    model = model or config.DEFAULTS["model"]
    output_config = {"format": {"type": "json_schema", "schema": api_schema(schema)}}
    if effort:
        output_config["effort"] = effort
    started = datetime.now()
    try:
        msg = client(key, timeout_s, http_client, max_retries).messages.create(
            model=model, max_tokens=int(max_tokens), output_config=output_config,
            messages=[{"role": "user", "content": prompt}])
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as err:
        return RunResult(False, error=f"Anthropic refused the API key ({err.status_code}): check it on the Settings "
                                      f"page", kind="login")
    except anthropic.RateLimitError as err:
        return RunResult(False, error=f"rate limited by the Anthropic API: {err.message}"[:600], kind="limit",
                         limit_until=_retry_until(err, default_pause_min))
    except anthropic.BadRequestError as err:
        text = str(err.message)
        if "credit balance" in text.lower():
            return RunResult(False, error="your Anthropic credit balance is too low: add credit at "
                                          "platform.claude.com, then the queue carries on", kind="login")
        return RunResult(False, error=f"the Anthropic API rejected the request: {text}"[:600], kind="error")
    except anthropic.NotFoundError as err:
        return RunResult(False, error=f"model {model!r} not found: pick another on the Settings page ({err.message})"
                         [:600], kind="error")
    except anthropic.APIStatusError as err:
        if err.status_code in (500, 502, 503, 504, 529):
            return RunResult(False, error=f"the Anthropic API is overloaded ({err.status_code})", kind="limit",
                             limit_until=_retry_until(err, OFFLINE_PAUSE_MIN))
        return RunResult(False, error=f"Anthropic API error {err.status_code}: {err.message}"[:600], kind="error")
    except anthropic.APIConnectionError as err:
        return RunResult(False, error=f"can't reach the Anthropic API ({err}); trying again in {OFFLINE_PAUSE_MIN} min",
                         kind="limit", limit_until=datetime.now().astimezone() + timedelta(minutes=OFFLINE_PAUSE_MIN))
    usage = _usage(msg)
    res = RunResult(True, raw={"id": msg.id, "stop_reason": msg.stop_reason, "model": msg.model}, usage=usage,
                    cost_usd=cost_usd(model, usage), model=msg.model or model, num_turns=1,
                    duration_ms=int((datetime.now() - started).total_seconds() * 1000))
    if msg.stop_reason == "refusal":
        res.ok, res.kind, res.error = False, "error", "Claude declined this request"
        return res
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    try:
        output = json.loads(text)
    except json.JSONDecodeError:
        output = None
    if not isinstance(output, dict):
        cut = " (the answer hit max_tokens; raise it in settings.json)" if msg.stop_reason == "max_tokens" else ""
        res.ok, res.kind, res.error = False, "error", "Claude did not return the JSON object the schema asks for" + cut
        return res
    res.output = output
    return res


def check(api_key: str | None = None, model: str | None = None, http_client=None, max_retries: int = 2) -> dict:
    """The Settings page's Test button: counts the tokens of a one-word prompt, which proves the key and the model
    work without spending anything (token counting is free)."""
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "the anthropic package is not installed: pip install anthropic"}
    key = api_key or config.anthropic_key()
    if not key:
        return {"ok": False, "error": "no API key saved"}
    model = model or config.settings().get("model") or config.DEFAULTS["model"]
    try:
        got = client(key, 30, http_client, max_retries).messages.count_tokens(
            model=model, messages=[{"role": "user", "content": "ping"}])
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return {"ok": False, "error": "Anthropic refused this key"}
    except anthropic.NotFoundError:
        return {"ok": False, "error": f"the key works, but model {model} was not found"}
    except anthropic.APIStatusError as err:
        return {"ok": False, "error": f"Anthropic API error {err.status_code}: {err.message}"[:300]}
    except anthropic.APIConnectionError as err:
        return {"ok": False, "error": f"can't reach the Anthropic API: {err}"[:300]}
    return {"ok": True, "detail": f"key works with {model} ({got.input_tokens} tokens counted, no charge)"}


def masked(key: str | None) -> str | None:
    """sk-ant-...ab12 for the Settings page."""
    if not key:
        return None
    return f"{key[:7]}...{key[-4:]}" if len(key) > 14 else "saved"
