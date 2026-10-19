"""The Claude backends, and the CLI one: headless Claude Code (`claude -p --output-format json --json-schema ...`)
with no tools, no MCP, no user settings, under the person's own CLAUDE_CONFIG_DIR. This one needs no API key: it
uses whatever Claude Code is logged in with on this machine (`claude auth login`). Never prints or stores
credentials.

Usage-limit and login problems are detected from the result text so the worker can pause or wait."""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIMIT_RE = re.compile(r"(hit your (?:usage |weekly |session )?limit|usage limit|rate limit|limit reached|limit exceeded|too many requests|overloaded|"
                      r"\b429\b|out of (?:extra )?usage|resets? (?:at|in)\b)", re.I)
LOGIN_RE = re.compile(r"(not logged in|please run /login|please log ?in|invalid api key|authentication_error|"
                      r"oauth token (?:expired|revoked)|no credentials|unauthori[sz]ed)", re.I)
_RESET_AT = re.compile(r"reset\w*\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_RESET_IN = re.compile(r"(?:reset\w*|try again|retry|available)\s*in\s*(?:about\s*)?(\d+)\s*"
                       r"(minutes?|mins?|hours?|hrs?|seconds?|s\b)", re.I)
_STRIP_ENV = ("RESUME_TAILOR_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
              "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_EXECPATH",
              "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CONFIG_DIR")


@dataclass
class RunResult:
    ok: bool
    output: dict | None = None
    raw: dict | None = None
    error: str | None = None
    kind: str = "ok"                       # ok | limit | login | error
    limit_until: datetime | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int | None = None
    model: str | None = None
    num_turns: int | None = None
    stderr: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "error": self.error, "usage": self.usage, "cost_usd": self.cost_usd,
                "duration_ms": self.duration_ms, "model": self.model, "num_turns": self.num_turns,
                "limit_until": self.limit_until.isoformat() if self.limit_until else None}


def env_for(config_dir: str | None) -> dict:
    """A clean environment: no nested-session markers from the Claude Code this worker may run under."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    return env


def account_status(config_dir: str | None, claude_bin: str = "claude") -> dict:
    """{logged_in, email, subscription, config_dir} from `claude auth status` (no secrets in that output)."""
    try:
        proc = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True, timeout=30,
                              env=env_for(config_dir))
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as err:
        return {"logged_in": False, "email": None, "subscription": None, "config_dir": config_dir,
                "error": str(err)[:200]}
    return {"logged_in": bool(data.get("loggedIn")), "email": data.get("email"),
            "subscription": data.get("subscriptionType"), "config_dir": data.get("configDirectory") or config_dir,
            "method": data.get("authMethod")}


def parse_reset(text: str, now: datetime | None = None, default_min: int = 30) -> datetime:
    """When a limit message says the quota comes back; `default_min` from now when it does not say."""
    now = now or datetime.now().astimezone()
    m = _RESET_IN.search(text or "")
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
        return now + timedelta(seconds=secs)
    m = _RESET_AT.search(text or "")
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        hour = hour % 12 + (12 if ampm == "pm" else 0)
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when
    return now + timedelta(minutes=default_min)


def classify(text: str) -> str:
    if LOGIN_RE.search(text or ""):
        return "login"
    if LIMIT_RE.search(text or ""):
        return "limit"
    return "error"


def _usage(raw: dict) -> dict:
    u = raw.get("usage") or {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    out = {k: u.get(k) for k in keys if k in u}
    details = u.get("output_tokens_details") or {}
    if "thinking_tokens" in details:
        out["thinking_tokens"] = details["thinking_tokens"]
    return out


def run(prompt: str, schema: dict, *, model: str | None = None, effort: str | None = None,
        config_dir: str | None = None, cwd: Path | None = None, timeout_s: int = 900,
        budget_usd: float | None = None, claude_bin: str = "claude", max_turns: int = 4,
        default_pause_min: int = 30) -> RunResult:
    cmd = [claude_bin, "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--no-session-persistence", "--max-turns", str(max_turns), "--permission-mode", "default"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if budget_usd:
        cmd += ["--max-budget-usd", str(budget_usd)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout_s,
                              cwd=str(cwd) if cwd else None, env=env_for(config_dir))
    except subprocess.TimeoutExpired:
        return RunResult(False, error=f"claude -p timed out after {timeout_s} s", kind="error")
    except OSError as err:
        return RunResult(False, error=f"cannot run {claude_bin}: {err}", kind="error")
    stderr = (proc.stderr or "")[-2000:]
    raw = _last_json(proc.stdout or "")
    if raw is None:
        text = (proc.stdout or "") + "\n" + stderr
        kind = classify(text)
        res = RunResult(False, error=f"claude exited {proc.returncode} without JSON: {text.strip()[-500:]}",
                        kind=kind, stderr=stderr)
        if kind == "limit":
            res.limit_until = parse_reset(text, default_min=default_pause_min)
        return res
    result_text = raw.get("result") if isinstance(raw.get("result"), str) else json.dumps(raw.get("result"))
    res = RunResult(True, raw=raw, usage=_usage(raw), cost_usd=raw.get("total_cost_usd"),
                    duration_ms=raw.get("duration_ms"), num_turns=raw.get("num_turns"), stderr=stderr,
                    model=next(iter((raw.get("modelUsage") or {}).keys()), None))
    if raw.get("is_error") or raw.get("subtype") not in (None, "success"):
        text = f"{raw.get('subtype')}: {result_text or ''} {stderr}"
        res.ok, res.kind, res.error = False, classify(text), text.strip()[-600:]
        if res.kind == "limit":
            res.limit_until = parse_reset(text, default_min=default_pause_min)
        return res
    output = raw.get("structured_output")
    if output is None and result_text:
        try:
            output = json.loads(result_text)
        except json.JSONDecodeError:
            output = _embedded_json(result_text)
    if not isinstance(output, dict):
        kind = classify(result_text or "")
        res.ok, res.kind = False, kind
        res.error = (result_text or "").strip()[-600:] if kind != "error" else \
            "Claude did not return the JSON object the schema asks for"
        if kind == "limit":
            res.limit_until = parse_reset(result_text or "", default_min=default_pause_min)
        return res
    res.output = output
    return res


def _last_json(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict) and data.get("type") == "result":
                    return data
            except json.JSONDecodeError:
                continue
    return None


def _embedded_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def pause_text(until: datetime) -> str:
    local = until.astimezone()
    today = datetime.now().astimezone().date()
    return f"Paused until {local.strftime('%-I:%M %p')}" + ("" if local.date() == today else local.strftime(" (%a)"))


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def runner_for(settings: dict):
    """The run() of the backend settings `claude_backend` names (api, cli or demo)."""
    backend = settings.get("claude_backend") or "api"
    if backend == "demo":
        from . import demo_runner
        return demo_runner.run
    if backend == "cli":
        return run
    from . import api_runner
    return lambda prompt, schema, **kw: api_runner.run(prompt, schema, max_tokens=settings.get("max_tokens") or 16000,
                                                       **kw)


def account(prof, settings: dict) -> dict:
    """{logged_in, email, method, error?} for the backend in use: the worker parks a person's jobs while False."""
    backend = settings.get("claude_backend") or "api"
    if backend == "demo":
        return {"logged_in": True, "email": None, "method": "demo"}
    if backend == "cli":
        return account_status(prof.config_dir if prof is not None else None, settings.get("claude_bin") or "claude")
    from . import config
    has_key = bool(config.anthropic_key())
    return {"logged_in": has_key, "email": None, "method": "api key",
            **({} if has_key else {"error": "no Anthropic API key"})}


def login_text(prof, settings: dict) -> str:
    """What a waiting job says until Claude is reachable again."""
    backend = settings.get("claude_backend") or "api"
    if backend == "cli":
        where = f"CLAUDE_CONFIG_DIR={prof.config_dir} " if prof is not None and prof.config_dir else ""
        return f"Claude Code is not logged in on this machine: run `{where}claude auth login` once"
    return "Claude: add or fix your Anthropic API key on the Settings page (or switch to demo mode)"
