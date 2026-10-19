"""One job, start to finish: pick the base by ATS score, ask Claude for structured JSON with the person's fact
bank, fill the base .docx, make the PDF, run the eight checks, then REPAIR rather than regenerate: mechanical fixes
first (no Claude), then repair rounds where Claude rewrites only the failing items, up to `max_repairs`; only then
"needs you". Every round's files stay under <home>/runs/<id>/attempt<n>/.
A "Fix with note" is ONE Claude call and then stops: the result is shown as is, with its problems."""
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import (ats, basefit, changes, checks, claude_runner, config, confirmed, db, diff, docxio, pdfx, places,
               profiles, prompts, render, repair, rules, versions)

log = logging.getLogger(__name__)
_MEASURES: dict = {}
_MEASURE_LOCK = threading.Lock()
MAX_MECHANICAL = 4            # free rounds per job (a mechanical fix that keeps failing hands over to Claude)
WRAP_TRIM_STEP = 4            # extra padding spaces removed when a date still wraps after the base fit


class Limit(Exception):
    def __init__(self, until: datetime, message: str):
        super().__init__(message)
        self.until = until


class Login(Exception):
    pass


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def engine_order(settings: dict) -> tuple:
    """settings `pdf_engine`: "auto" = what this machine has (LibreOffice, then Word, then Google Docs);
    "libreoffice", "word" or "google" = that engine ("word" falls back to Google; "google" falls back to Word only
    with `word_fallback: true`, since Word may stop to ask for file access)."""
    first = str(settings.get("pdf_engine") or "auto").lower()
    if first == "google":
        return ("google", "word") if settings.get("word_fallback") else ("google",)
    if first == "word":
        return ("word", "google")
    if first == "libreoffice":
        return ("libreoffice",)
    local = tuple(e for e, ok in (("libreoffice", render.libreoffice_available(settings)),
                                  ("word", render.word_available())) if ok)
    return local + ("google",)


def base_fit(base, engine: str = "word", google=None) -> basefit.Fit:
    """The engine-specific base fit (Google: date padding trimmed so no date wraps) and measure, once per base."""
    return _fit_and_measure(base, engine, google)[0]


def _fit_and_measure(base, engine: str, google) -> tuple:
    key = (base.sha, engine if engine != "google" or google is not None else "own")
    with _MEASURE_LOCK:                       # one fit per base and engine, even with two queues running
        found = _MEASURES.get(key)
        if found is not None:
            return found
        doc = docxio.read(base.docx)
        try:
            fit = basefit.fit(base, doc, google, engine)
        except basefit.FitError as err:       # transient trouble: use the base's own PDF now, retry next job
            if base.pdf is None:
                raise render.RenderError(str(err)) from err
            log.warning("base %s: %s; measuring the base's own PDF for this job", base.id, err)
            return basefit.Fit(engine, {}, None), checks.measure(base.pdf, base.structure, doc)
        pdf = fit.pdf or base.pdf
        if pdf is None:
            raise render.RenderError(f"no PDF of the base {base.label}: the {engine} engine is not available "
                                     f"(Settings > PDF engine)")
        found = _MEASURES[key] = (fit, checks.measure(pdf, base.structure, doc))
        return found


def base_pdf(base, fit) -> Path | None:
    """The base as the engine in use draws it (its own PDF when the engine used it as is)."""
    return fit.pdf or base.pdf


def measure_base(base, engine: str = "word", google=None) -> checks.Measure:
    return _fit_and_measure(base, engine, google)[1]


def _resume_entries(prof, extra_text: str | None = None, families=()) -> list:
    out = [{"id": b.id, "label": b.label, "text": b.text, "families": list(b.families)} for b in prof.bases]
    if extra_text is not None:
        out.append({"id": "tailored", "label": "Tailored", "text": extra_text, "families": list(families)})
    return out


def _score(prof, job: dict, extra_text: str | None, era: dict | None = None, families=()) -> dict | None:
    """The keyword model's scores (ats.py) for every base and, when given, the tailored text. The tailored text
    carries its base's title families so a tie between bases breaks the same way for it."""
    try:
        return ats.score(job["jd_text"], _resume_entries(prof, extra_text, families), title=job["role"],
                         company=job["company"], jd_url=job.get("jd_url"), profile_years=prof.years,
                         extra_texts={"known": prof.known_text(), **(era or {})})
    except Exception as err:              # a scoring fault must not lose the job: the ATS check reports it
        log.warning("job %s: scoring failed: %s", job["id"], err)
        return None


def choose_base(prof, job: dict, scores: dict | None):
    """(base, why): the person's pick, else the best keyword score (ties: the base titled like the job)."""
    override = prof.base(job.get("base_override"))
    by_id = {s["id"]: s for s in (scores or {}).get("scores") or []}
    if override is not None:
        pct = by_id.get(override.id, {}).get("pct")
        return override, f"your pick ({pct}% match)" if pct is not None else "your pick"
    best_id = (scores or {}).get("best_id")
    base = prof.base(best_id) or prof.bases[0]
    if not by_id:
        return base, "first base (ATS scoring unavailable)"
    ranked = sorted(by_id.values(), key=lambda s: -(s.get("pct") or 0))
    listing = ", ".join(f"{s['label']} {s.get('pct')}%" for s in ranked)
    return base, f"best {scores.get('vendor_label') or 'keyword'} match: {listing}"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (bytes, bytearray)):
        path.write_bytes(data)
    elif isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def _check_cancel(conn, job_id: int) -> None:
    if db.cancel_requested(conn, job_id):
        raise Cancelled()


def _attempt_record(n: int, started: float, res, failures: list, engine: str | None, files: dict,
                    kind: str = "full", notes: list | None = None) -> dict:
    return {"n": n, "kind": kind, "started": datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished": _now(), "duration_s": round(time.time() - started, 1),
            "model": getattr(res, "model", None), "usage": getattr(res, "usage", {}),
            "cost_usd": getattr(res, "cost_usd", None), "claude_ms": getattr(res, "duration_ms", None),
            "num_turns": getattr(res, "num_turns", None), "failures": failures, "engine": engine, "files": files,
            "notes": list(notes or [])}


@dataclass(frozen=True)
class Context:
    """Everything one build-and-check needs that does not change between rounds."""
    conn: object
    job: dict
    prof: object
    base: object
    measure: checks.Measure
    base_doc: docxio.Doc
    settings: dict
    google: object
    scores0: dict | None
    fit: basefit.Fit = basefit.Fit("word", {}, None)
    notes: tuple = ()                 # this job's Fix-with-note texts (what they name is user-confirmed)

    @property
    def prules(self) -> dict:
        """The person's rules for this job, with its headline and summary opening."""
        prules = rules.for_profile(self.prof.id, self.prof.rules)
        return rules.for_job(prules, self.job.get("role"), self.base.structure.headline, self.job.get("location"))


@dataclass(frozen=True)
class Built:
    report: checks.Report
    tailored: object
    engine: str
    files: dict

    @property
    def failures(self) -> list:
        return [f.message for f in self.report.failures]


_OPENING = re.compile(r"^(?P<title>[^.]{1,60}?) with (?P<years>\d\+? years)\b")


def fixed_opening(summary: str, start: str | None) -> str:
    """repair: a summary opening "<another title> with 4 years" gets the required "<headline> with 4 years";
    anything else is left for the checks (and Claude's repair round)."""
    if not start or not summary or summary.startswith(start):
        return summary
    found = _OPENING.match(summary)
    if not found or not start.endswith(found.group("years")):
        return summary
    return start + summary[found.end():]


def locked_headline(content: dict, base, prules: dict) -> dict:
    """/ when the person's rules lock the title under the name, it is the job's headline (the
    plain title for the posting's family, else the base's own title) whatever Claude wrote, and the summary opens with it (a new
    dict; the checks still verify the rendered .docx)."""
    if prules.get("headline_may_change", True):
        return content
    want = rules.headline_of(prules, base.structure.headline)
    summary = fixed_opening(content.get("summary") or "", rules.summary_start(prules, getattr(base, "id", None)))
    if content.get("headline") == want and summary == (content.get("summary") or ""):
        return content
    return {**content, "headline": want, **({"summary": summary} if content.get("summary") else {})}


def build_and_check(ctx: Context, content: dict, folder: Path, files: dict, extra_trim: dict | None = None,
                    lock: bool = True, on_step=None) -> Built:
    """content JSON -> refilled base .docx (date padding trimmed for the engine) -> PDF -> the 8 checks (+ page
    image). Raises docxio.DocxError when the content does not fit the base layout and render.RenderError when no
    PDF engine worked. lock=False: a person's own edit (Re-import), whose title is never overwritten.
    content["user_location"]: the place the person chose for the contact line (a note or their Doc) wins over
    the job's own."""
    if content.get("user_location"):
        ctx = replace(ctx, job={**ctx.job, "location": content["user_location"]})
    job, prof, base = ctx.job, ctx.prof, ctx.base
    if lock:
        content = locked_headline(content, base, ctx.prules)
    place = rules.contact_location(ctx.prules)                   # the job's city on the contact line
    content = {**content, "contact_location": place} if place else content
    trim = {**ctx.fit.trim}
    for k, v in (extra_trim or {}).items():
        trim[k] = trim.get(k, 0) + v
    docx_bytes = docxio.fill(ctx.base_doc, content, pad_trim=trim)
    docx_path = folder / "resume.docx"
    _write(docx_path, docx_bytes)
    files = {**files, "docx": str(docx_path)}
    title = f"{prof.name} {job['role']} ({job['company']})".replace("/", "-")
    tailored = docxio.structure(docxio.read(docx_bytes))

    def dates_intact(pdf: bytes) -> str | None:
        probe = folder / "candidate.pdf"
        probe.write_bytes(pdf)
        try:
            problems = checks.date_problems(pdfx.flat_lines(pdfx.lines(probe)), tailored)
        except pdfx.PdfError as err:
            return str(err)
        finally:
            probe.unlink(missing_ok=True)
        return "; ".join(problems) or None

    pdf_bytes, engine = render.to_pdf(docx_bytes, title, folder, ctx.google, docx_path,
                                      order=engine_order(ctx.settings), validate=dates_intact)
    pdf_path = folder / "resume.pdf"
    _write(pdf_path, pdf_bytes)
    files["pdf"] = str(pdf_path)
    if on_step:                                                  # the Re-import busy line follows the steps
        on_step("checking")
    report = check_files(ctx, docx_path, pdf_path, tailored)
    _write(folder / "checks.json", report.as_dict())
    try:
        files["png"] = str(pdfx.png(pdf_path, folder / "page-1", ctx.settings.get("pdf_dpi", 110)))
    except pdfx.PdfError as err:
        log.warning("job %s: page image failed: %s", job["id"], err)
    return Built(report, tailored, engine, files)


def check_files(ctx: Context, docx_path: Path, pdf_path: Path, struct) -> checks.Report:
    """The 8 checks, with today's rules, on a .docx + PDF already made (no render, no Claude)."""
    job, prof, base = ctx.job, ctx.prof, ctx.base
    scores = _score(prof, job, struct.full_text(), checks.era_texts(struct, prof.id, prof.rules), base.families)
    inp = checks.Input(prof.id, base.structure, ctx.base_doc, ctx.measure, docx_path, pdf_path, job["jd_text"],
                       prof.known_skills, scores, base.id, [b.text for b in prof.bases], prof.skill_text, prof.rules,
                       job.get("role") or "", job.get("location") or "", tuple(ctx.notes))
    return checks.run_all(inp)


NOTE_KEYS = ("review", "match", "gaps", "change_notes")      # Claude's notes on the resume (not in a .docx)


def _keep_notes(fields: dict, previous: dict) -> dict:
    """A re-import reads an edited Doc, which has no notes: keep the ones Claude wrote for this resume."""
    return {**fields, **{k: previous[k] for k in NOTE_KEYS if fields.get(k) is None and previous.get(k) is not None}}


def _result_fields(ctx: Context, built: Built, content: dict, n: int, model: str | None) -> dict:
    report = built.report
    added = set(content.get("added_from_jd") or []) | set(report.info.get("honesty", {}).get("added_from_jd") or [])
    return {"final_attempt": n, "checks": report.as_dict(), "ats": report.info.get("ats"),
            "review": content.get("review"), "match": content.get("match"), "gaps": content.get("gaps"),
            "change_notes": content.get("change_notes"), "added_from_jd": sorted(added),
            "diff": diff.bullet_diff(ctx.base.structure, built.tailored), "engine": built.engine,
            "model": model, "headline": built.tailored.headline}


# ---------- the attempt / repair loop ----------

@dataclass
class State:
    """Mutable loop state of one job (the Context is frozen)."""
    result: dict
    content: dict | None = None
    n: int = 0                        # rounds so far (attempt folders)
    full_tries: int = 0
    repairs: int = 0
    mechanical: int = 0
    failures: list = field(default_factory=list)
    report: checks.Report | None = None
    model: str | None = None
    extra_trim: dict = field(default_factory=dict)


def _archive_previous(run_dir: Path) -> None:
    """A fresh run keeps the earlier run's files under prev-<stamp>/ instead of overwriting attempt1/."""
    old = [p for p in run_dir.iterdir() if p.is_dir() and (p.name.startswith("attempt") or p.name.startswith("reimport"))]
    old += versions.history_dirs(run_dir)      # a fresh run starts a new Undo history
    if not old:
        return
    target = run_dir / f"prev-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    target.mkdir(parents=True, exist_ok=True)
    for p in old:
        shutil.move(str(p), str(target / p.name))


def _resume_state(job: dict, run_dir: Path, fresh: dict) -> State:
    """After a usage-limit pause the job comes back with its attempts kept: continue from the last content in
    repair mode instead of paying for the whole resume again."""
    result = db.result_of(job)
    records = result.get("attempts") or []
    if int(job.get("attempts") or 0) <= 0 or not records:
        _archive_previous(run_dir)
        return State(fresh)
    st = State(result, n=max(int(r.get("n") or 0) for r in records))
    counted = [r for r in records if not any(str(f).startswith(("usage limit:", "login:"))
                                             for f in r.get("failures") or [])]    # pauses are not rounds
    st.full_tries = sum(1 for r in counted if r.get("kind", "full") in ("full", "attempt"))
    st.repairs = 0                            # a resumed run (after a pause, or re-queued by hand) gets a fresh
    st.mechanical = 0                         # repair budget; the earlier rounds stay on record
    st.extra_trim = {int(k): int(v) for k, v in (result.get("extra_trim") or {}).items()}
    last = next((r for r in reversed(records) if (r.get("files") or {}).get("content")
                 and (Path(r["files"]["content"]).parent / "checks.json").exists()), None)
    if last is None:
        return st
    try:
        st.content = json.loads(Path(last["files"]["content"]).read_text())
        folder = Path(last["files"]["content"]).parent
        st.report = checks.Report.from_dict(json.loads((folder / "checks.json").read_text()))
        st.failures = [f.message for f in st.report.failures]
        st.model = last.get("model")
    except (OSError, ValueError, KeyError):
        st.content, st.report = None, None
    if st.report is None:
        st.content = None
    return st


def _call(ctx: Context, st: State, runner, prompt: str, schema: dict, folder: Path):
    _write(folder / "prompt.txt", prompt)
    s = ctx.settings
    res = runner(prompt, schema, model=s.get("model"), effort=s.get("effort"), config_dir=ctx.prof.config_dir,
                 cwd=config.ROOT, timeout_s=int(s.get("claude_timeout_s") or 900), budget_usd=s.get("max_budget_usd"),
                 claude_bin=s.get("claude_bin") or "claude", default_pause_min=int(s.get("pause_default_min") or 30))
    _write(folder / "claude.json", {"result": res.as_dict(), "raw": res.raw, "stderr": res.stderr})
    return res


def _record(ctx: Context, st: State, record: dict, note: str | None) -> None:
    st.result["attempts"].append(record)
    db.update(ctx.conn, ctx.job["id"], result=json.dumps(st.result))
    if note:
        db.event(ctx.conn, ctx.job["id"], ctx.prof.id, "note", note[:400])


def _start_round(ctx: Context, st: State, kind: str) -> Path:
    st.n += 1
    run_dir = config.RUNS / str(ctx.job["id"])
    while (run_dir / f"attempt{st.n}").exists():      # never reuse a round's folder (after Undo the restored record
        st.n += 1                                     # count is lower than the folders on disk)
    folder = run_dir / f"attempt{st.n}"
    folder.mkdir(parents=True)
    db.update(ctx.conn, ctx.job["id"], attempts=st.n, status="generating")
    db.event(ctx.conn, ctx.job["id"], ctx.prof.id, "status", f"{kind} {st.n}: generating",
             {"status": "generating", "attempt": st.n})
    return folder


def _claude_problem(ctx: Context, st: State, res, started: float, kind: str) -> bool:
    """Limit / login raise for the worker; a non-JSON answer records the round and returns True."""
    if res.kind == "limit":
        _record(ctx, st, _attempt_record(st.n, started, res, [f"usage limit: {res.error}"], None, {}, kind), None)
        raise Limit(res.limit_until, res.error or "usage limit")
    if res.kind == "login":
        _record(ctx, st, _attempt_record(st.n, started, res, [f"login: {res.error}"], None, {}, kind), None)
        raise Login(res.error or "not logged in")
    if not res.ok:
        problem = f"Claude did not answer with usable JSON: {res.error}"
        if kind not in ("repair", "fix") or not st.failures:
            st.failures = [problem]
        _record(ctx, st, _attempt_record(st.n, started, res, [problem], None, {}, kind), f"{kind} {st.n}: {problem}")
        return True
    return False


def _full_round(ctx: Context, st: State, runner, why: str) -> tuple:
    """Attempt 1 (and a fresh attempt whenever Claude's answer could not be used at all)."""
    st.full_tries += 1
    folder = _start_round(ctx, st, "attempt")
    started = time.time()
    job = ctx.job
    prompt = prompts.build(ctx.prof, ctx.base, ctx.measure, job["jd_text"], job["company"], job["role"],
                           ctx.scores0, st.failures, job.get("note"), st.n, why)
    res = _call(ctx, st, runner, prompt, prompts.SCHEMA, folder)
    if _claude_problem(ctx, st, res, started, "attempt"):
        return None, res, started, folder
    return res.output, res, started, folder


def _repair_round(ctx: Context, st: State, runner, plan_: repair.Plan) -> tuple:
    st.repairs += 1
    folder = _start_round(ctx, st, "repair")
    started = time.time()
    job = ctx.job
    prompt = prompts.build_repair(ctx.prof, ctx.base, ctx.measure, st.content, plan_, job["jd_text"],
                                  job["company"], job["role"], ctx.scores0, st.repairs)
    _write(folder / "plan.json", {"items": [i.__dict__ for i in plan_.items], "keywords": plan_.keywords,
                                  "shorten_lines": plan_.shorten_lines, "unrepairable": plan_.unrepairable,
                                  "notes": plan_.notes})
    res = _call(ctx, st, runner, prompt, prompts.REPAIR_SCHEMA, folder)
    if _claude_problem(ctx, st, res, started, "repair"):
        return None, res, started, folder, []
    content, notes = repair.apply(st.content, res.output, plan_)
    return content, res, started, folder, notes


def _note_round(ctx: Context, st: State, runner, note: str) -> str:
    """"Fix with note": ONE Claude call that changes only what the user's note names, then the
    build and the checks, and nothing else: no repair or mechanical round may undo or weaken what the note asked."""
    folder = _start_round(ctx, st, "fix")
    started = time.time()
    job, before = ctx.job, st.content
    shown = before.get("user_location") or job.get("location") or ctx.prules.get("location")
    prompt = prompts.build_note(ctx.prof, ctx.base, ctx.measure, before, note, st.failures, job["jd_text"],
                                job["company"], job["role"], shown)
    plan_ = repair.note_plan(before, note)
    res = _call(ctx, st, runner, prompt, prompts.NOTE_SCHEMA, folder)
    if _claude_problem(ctx, st, res, started, "fix"):
        return _note_done(ctx, st, note, [], None, "Claude did not answer with a usable change")
    output = res.output or {}
    content, notes = repair.apply(before, output, plan_)
    place = " ".join(str(output.get("location") or "").split())
    if place and place != shown:
        content = {**content, "user_location": place}
    content = locked_headline(content, ctx.base, ctx.prules)
    said, cannot = changes.describe(before, content), (output.get("cannot") or "").strip() or None
    db.event(ctx.conn, job["id"], ctx.prof.id, "note", f"fix {st.n}: " + ("; ".join(said) or "no change")[:400])
    if not said:
        _record(ctx, st, _attempt_record(st.n, started, res, ["the note changed nothing"], None, {}, "fix", notes),
                None)
        return _note_done(ctx, st, note, [], cannot, "the note changed nothing")
    outcome = _build_round(ctx, st, content, res, started, folder, "fix", notes)
    if outcome == "failed":
        return "failed"
    if st.content is before:                  # the answer did not fit the base layout: the resume stays as it was
        return _note_done(ctx, st, note, [], cannot, "the change did not fit the base layout")
    return _note_done(ctx, st, note, said, cannot, None, before)


def _body(content: dict | None) -> str:
    content = content or {}
    return " ".join([content.get("summary") or ""] + [b for j in content.get("jobs") or [] for b in j["bullets"]])


def _new_fact(note: str, before: dict | None, after: dict | None) -> bool:
    """The note states a number about the person (not "bullet 3" / "25 words") that the note's change put into the
    resume: the note is then kept as one of their facts."""
    stated = checks._numbers(confirmed.fact_text(note))
    return bool(stated & (checks._numbers(_body(after)) - checks._numbers(_body(before))))


def _note_done(ctx: Context, st: State, note: str, said: list, cannot: str | None, not_done: str | None,
               before: dict | None = None) -> str:
    """Record what the note did, save what it confirmed and settle the status: ready when the note's
    change passes every check, else needs you with the problems listed (the person picks what next)."""
    job_id, pid = ctx.job["id"], ctx.prof.id
    honesty = (st.report.info.get("honesty") or {}) if st.report is not None and not not_done else {}
    terms = list(honesty.get("confirmed_by_note") or [])
    saved = confirmed.add(pid, "skills", terms, job_id) if terms else []
    if not not_done and _new_fact(note, before, st.content):
        saved += confirmed.add(pid, "facts", [note], job_id)
    st.result["fix"] = {"note": note, "at": _now(), "applied": not not_done, "changes": said, "cannot": cannot,
                        "confirmed": terms, "saved": saved, "not_done": not_done}
    if not not_done:
        st.result["notes_confirmed"] = list(ctx.notes)
    db.update(ctx.conn, job_id, result=json.dumps(st.result))
    if saved:
        db.event(ctx.conn, job_id, pid, "note", f"saved to {ctx.prof.name}'s confirmed list: " + ", ".join(saved))
    if not not_done and st.report is not None and st.report.ok:
        return "ready"                        # _build_round set it (and told the person)
    if not_done:
        versions.discard_newest(job_id, "fix", note)   # nothing new to undo: the version on screen is the kept one
        if st.result.get("fix_from") == "ready":     # a Ready (maybe approved) resume stays ready, as it was
            db.set_status(ctx.conn, job_id, "ready", f"your note was not applied ({not_done}); the resume is as it was")
            return "ready"
    problems = list(st.failures) if st.report is not None else []
    head = f"Your note was not applied ({not_done}{': ' + cannot if cannot else ''}); the resume is as it was" \
        if not_done else (f"Not done: {cannot}" if cannot else "")
    reason = "; ".join([p for p in [head] + problems if p]) or "your note is done; check it"
    db.set_status(ctx.conn, job_id, "needs_you", "your note is done: " + (f"{len(problems)} problem(s) to check"
                                                                          if problems else "check it"),
                  error=reason[:900])
    return "needs_you"


def _build_round(ctx: Context, st: State, content: dict, res, started: float, folder: Path, kind: str,
                 notes: list) -> str:
    """Fill, render, check, record. Returns ready | failed_checks | layout | failed."""
    job_id, prof = ctx.job["id"], ctx.prof
    content = locked_headline(content, ctx.base, ctx.prules)
    _write(folder / "content.json", content)
    _check_cancel(ctx.conn, job_id)
    db.update(ctx.conn, job_id, status="checking")
    db.event(ctx.conn, job_id, prof.id, "status", f"{kind} {st.n}: checking", {"status": "checking", "attempt": st.n})
    files = {"content": str(folder / "content.json")}
    if (folder / "prompt.txt").exists():
        files["prompt"] = str(folder / "prompt.txt")
    try:
        built = build_and_check(ctx, content, folder, files, st.extra_trim)
    except docxio.DocxError as err:
        problem = f"the JSON does not fit the base layout: {err}"
        _record(ctx, st, _attempt_record(st.n, started, res, [problem], None, files, kind, notes),
                f"{kind} {st.n}: {problem}")
        if kind == "full":
            st.failures = [problem]
            return "layout"
        return "failed_checks"                 # keep the previous content and report; this round is dropped
    except render.RenderError as err:
        _record(ctx, st, _attempt_record(st.n, started, res, [f"PDF: {err}"], None, files, kind, notes), None)
        db.set_status(ctx.conn, job_id, "failed", "PDF conversion failed", error=f"PDF conversion failed: {err}")
        return "failed"
    st.content, st.report, st.failures = content, built.report, built.failures
    st.model = getattr(res, "model", None) or st.model
    st.result["attempts"].append(_attempt_record(st.n, started, res, st.failures, built.engine, built.files, kind,
                                                 notes))
    st.result.update(_result_fields(ctx, built, content, st.n, st.model))
    st.result["files"].update({k: built.files[k] for k in versions.FILE_KEYS if k in built.files})
    st.result["files_at"] = _now()            # the file URLs carry it, so no screen keeps an old copy
    db.update(ctx.conn, job_id, result=json.dumps(st.result), model=st.model or ctx.settings.get("model"))
    if built.report.ok:
        db.set_status(ctx.conn, job_id, "ready", f"ready after {st.n} round{'s' if st.n > 1 else ''}")
        db.event(ctx.conn, job_id, prof.id, "toast", f"{prof.name}: {ctx.job['company']} · {ctx.job['role']} is ready",
                 {"job_id": job_id})
        return "ready"
    db.event(ctx.conn, job_id, prof.id, "note", f"{kind} {st.n}: {len(st.failures)} check failure(s): "
                                                 + "; ".join(st.failures)[:400])
    return "failed_checks"


def _wrap_trims(ctx: Context, st: State) -> dict:
    """Extra padding trim for every header whose date still wrapped (Google), on top of the base fit."""
    out = {}
    if engine_order(ctx.settings)[0] not in ("google", "libreoffice") or st.report is None:
        return out
    wrapped = [f.message for f in st.report.failures if f.name == "date_wrap"]
    structure = ctx.base.structure
    paras = {p.index: p.el for p in ctx.base_doc.paras}

    def can_shrink(idx: int) -> bool:
        used = ctx.fit.trim.get(idx, 0) + st.extra_trim.get(idx, 0)
        return docxio.trimmable(paras[idx]) - used > 0
    for msg in wrapped:
        owner = msg.split(" of ", 1)[1].split(" wraps")[0] if " of " in msg else ""
        for job in structure.jobs:
            if job["company"] == owner and can_shrink(job["header_para"]):
                out[job["header_para"]] = WRAP_TRIM_STEP
        if owner == "Education":
            for para in ctx.base_doc.paras:
                if para.kind == "edu" and checks._DATE_RE.search(para.text) and can_shrink(para.index):
                    out[para.index] = WRAP_TRIM_STEP
    return out


def _mechanical_round(ctx: Context, st: State) -> str | None:
    """Fix what Python can fix for free, re-render and re-check. None = nothing to do."""
    fix = repair.mechanical(st.content, st.report, ctx.prules, ctx.job.get("jd_text") or "")
    wrap = _wrap_trims(ctx, st)
    if not fix.changed and not wrap:
        return None
    if st.mechanical >= MAX_MECHANICAL:
        return None
    st.mechanical += 1
    notes = list(fix.notes) + [f"trimmed {v} more padding spaces on paragraph {k} (date wrapped)"
                               for k, v in wrap.items()]
    for k, v in wrap.items():
        st.extra_trim = {**st.extra_trim, k: st.extra_trim.get(k, 0) + v}
    st.result["extra_trim"] = {str(k): v for k, v in st.extra_trim.items()}
    folder = _start_round(ctx, st, "mechanical")
    db.event(ctx.conn, ctx.job["id"], ctx.prof.id, "note", f"mechanical {st.n}: " + "; ".join(notes)[:400])
    return _build_round(ctx, st, fix.content, None, time.time(), folder, "mechanical", notes)


def place_from_jd(conn, job: dict) -> dict:
    """A pasted JD (no tracker job, so no workplace sent) gets its workplace and contact-line city from the JD text,
    once; jobs from a tracker keep what the tracker sent."""
    if job.get("tracker_job_id") or job.get("workplace"):
        return job
    found = places.place(job.get("jd_text") or "")
    workplace = found.get("workplace") if found.get("workplace") in ("remote", "hybrid", "onsite") else None
    location = found.get("location") if workplace != "remote" else None
    db.update(conn, job["id"], workplace=workplace, location=location)
    db.event(conn, job["id"], job["profile"], "note", f"location from the JD: {found.get('why')}")
    return {**job, "workplace": workplace, "location": location}


def process(conn, job: dict, settings: dict | None = None, google=None, runner=None) -> str:
    """Runs one claimed job. Returns the final status. Raises Limit / Login / Cancelled for the worker.
    `runner` (tests) replaces the Claude backend."""
    settings = settings or config.settings()
    runner = runner or claude_runner.runner_for(settings)
    job_id = job["id"]
    job = place_from_jd(conn, job)
    prof = profiles.load(job["profile"])
    run_dir = config.RUNS / str(job_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    scores0 = _score(prof, job, None)
    base, why = choose_base(prof, job, scores0)
    engine = engine_order(settings)[0]
    try:
        fit = base_fit(base, engine, google)
        measure = measure_base(base, engine, google)
    except render.RenderError as err:
        db.set_status(conn, job_id, "failed", "PDF engine missing", error=f"PDF: {err}"[:900])
        return "failed"
    base_doc = docxio.read(base.docx)
    ctx = Context(conn, job, prof, base, measure, base_doc, settings, google, scores0, fit)
    db.update(conn, job_id, base_id=base.id, base_why=why, model=settings.get("model"))
    db.event(conn, job_id, prof.id, "note", f"base: {base.label} ({why})"
             + (f"; {fit.engine} fit: {fit.trim}" if fit.trim else ""))
    base_png = run_dir / "base-page-1.png"
    if not base_png.exists():
        pdfx.png(base_pdf(base, fit), run_dir / "base-page-1", settings.get("pdf_dpi", 110))
    base_pct = next((s.get("pct") for s in (scores0 or {}).get("scores") or [] if s["id"] == base.id), None)
    fresh = {"base": {"id": base.id, "label": base.label, "why": why, "pct": base_pct, "sha": base.sha}, "attempts": [],
             "files": {"base_pdf": str(base_pdf(base, fit)), "base_png": str(base_png)}, "fit": fit.as_dict()}
    st = _resume_state(job, run_dir, fresh)
    if st.result is fresh:                    # a restart mid-round must resume THIS run, not the archived one
        db.update(conn, job_id, result=json.dumps(fresh))
    if st.content is not None:
        db.event(conn, job_id, prof.id, "note", f"resuming after round {st.n} from its content (repair mode)")
    note = (job.get("fix_note") or "").strip()
    if not note:
        return _loop(ctx, st, runner, why)
    status = _fix_run(ctx, st, runner, why, note)
    db.clear_fix_note(conn, job_id)
    return status


def _fix_run(ctx: Context, st: State, runner, why: str, note: str) -> str:
    """Fix with note: ONE Claude call, then stop. max_claude_calls never
    blocks it (it is its own single call). A job with no resume to edit gets one full attempt with the note as its
    instruction; that too is shown as it comes out."""
    ctx = replace(ctx, notes=tuple(dict.fromkeys([*(st.result.get("notes_confirmed") or []), note])))
    if st.content is not None and st.report is not None:
        return _note_round(ctx, st, runner, note)
    ctx = replace(ctx, job={**ctx.job, "note": note})
    content, res, started, folder = _full_round(ctx, st, runner, why)
    if content is None:
        return _note_done(ctx, st, note, [], None, "Claude did not answer with a usable resume")
    outcome = _build_round(ctx, st, content, res, started, folder, "full", [])
    if outcome == "failed":
        return "failed"
    if outcome == "layout":
        return _note_done(ctx, st, note, [], None, "the answer did not fit the base layout")
    return _note_done(ctx, st, note, ["A new resume made with your note"], None, None)


def _loop(ctx: Context, st: State, runner, why: str) -> str:
    """The round loop: full attempt, then mechanical fixes and repair rounds, within max_claude_calls."""
    settings, conn, job_id = ctx.settings, ctx.conn, ctx.job["id"]
    max_full = int(settings.get("max_attempts") or 3)
    max_repairs = int(settings.get("max_repairs") if settings.get("max_repairs") is not None else 5)
    allowed = repair.allowed_keywords(ctx.scores0, ctx.prof.known_skills, [b.id for b in ctx.prof.bases],
                                      bool(ctx.prules.get("jd_skills_allowed")))
    stop_reason = None
    # at most N Claude calls per resume (full + repair), then "Needs you" for the person to approve or fix
    max_calls = int(settings.get("max_claude_calls") or 0)
    while True:
        _check_cancel(conn, job_id)
        if max_calls and st.full_tries + st.repairs >= max_calls:
            stop_reason = stop_reason or f"stopped after {st.full_tries + st.repairs} Claude tries (limit {max_calls})"
            break
        if st.content is None or st.report is None:
            if st.full_tries >= max_full:
                stop_reason = stop_reason or f"no usable resume after {st.full_tries} full attempts"
                break
            content, res, started, folder = _full_round(ctx, st, runner, why)
            if content is None:
                continue
            outcome = _build_round(ctx, st, content, res, started, folder, "full", [])
        else:
            plan_ = repair.plan(st.report, st.content, ctx.measure, ctx.prules, allowed)
            if plan_.empty:
                stop_reason = "; ".join(plan_.unrepairable + plan_.notes) or "no repairable item in the failures"
                break
            if st.repairs >= max_repairs:
                stop_reason = f"still failing after {st.repairs} repair rounds"
                break
            content, res, started, folder, notes = _repair_round(ctx, st, runner, plan_)
            if content is None:
                continue
            outcome = _build_round(ctx, st, content, res, started, folder, "repair", notes)
        if outcome == "ready":
            return "ready"
        if outcome == "failed":
            return "failed"
        if outcome == "layout":
            st.content, st.report = None, None
            continue
        mech = _mechanical_round(ctx, st)
        if mech == "ready":
            return "ready"
        if mech == "failed":
            return "failed"
        if mech == "layout":
            st.content, st.report = None, None
    reason = "; ".join(st.failures)[:800] or stop_reason or "no usable answer from Claude"
    if stop_reason and stop_reason not in reason:
        reason = f"{reason} ({stop_reason})"[:900]
    db.set_status(conn, job_id, "needs_you", f"needs you after {st.n} rounds", error=reason)
    return "needs_you"


def account_for(prof) -> dict:
    return claude_runner.account(prof, config.settings())
