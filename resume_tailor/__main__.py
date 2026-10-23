"""CLI: python -m resume_tailor <command>

  serve [--lan] [--port N] [--no-worker]   the UI + API on port 8770, with the queue worker in the same process
  demo [--port N]              try it with no keys: a separate demo home, a fictional profile, no AI calls
  worker [--once]              the queue worker on its own (--once drains the queue and exits)
  add -p PROFILE -c COMPANY -r ROLE (--jd-file F | --jd-url U) [--base ID]
  run JOB_ID                   process one queued job in this process (no worker needed)
  status                       queue overview
  doctor                       what is set up: Claude, PDF engines, poppler, Google, profiles
  set-model MODEL|default      pick the model (settings.json)
  idle-cpu [--seconds N]       measure the worker's CPU while idle
  token                        print the URL with the access token
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, db, profiles

def cmd_status(args) -> int:
    conn = db.connect()
    for pid in profiles.ids():
        pause = db.pause_of(conn, pid)
        print(f"== {pid}" + (f"  PAUSED until {pause['until']} ({pause['reason']})" if pause else ""))
        for j in db.list_jobs(conn, pid, limit=50):
            el = f"{j['elapsed_s']:.0f}s" if j.get("elapsed_s") else ""
            print(f"  #{j['id']:<4} {j['status']:<10} a{j['attempts']} {el:>6}  {j['company']} · {j['role']}"
                  + (f"  [{j['error'][:80]}]" if j.get("error") else ""))
    return 0


def cmd_doctor(args) -> int:
    from . import setup_status, worker
    st = setup_status.status(config.settings(), worker.alive())
    print("home:", st["home"])
    for key in ("claude", "pdf", "google", "profiles", "worker"):
        print(f"{key + ':':<10} {st[key]['state']:<9} {st[key]['detail']}")
    return 0


def cmd_add(args) -> int:
    from . import jd, server, worker
    try:
        text = Path(args.jd_file).read_text() if args.jd_file else jd.fetch(args.jd_url)["text"]
        fields = server.Handler._validate_job(None, {"profile": args.profile, "company": args.company, "role": args.role,
                                                     "jd_text": text, "jd_url": args.jd_url, "base_override": args.base},
                                              set(profiles.ids()))
    except (jd.JdError, ValueError, OSError) as err:
        print("not queued:", err)
        return 1
    jid = db.add_job(db.connect(), *fields)
    print(f"queued job {jid}; worker awake: {worker.wake()}")
    return 0


def cmd_run(args) -> int:
    from . import worker
    conn = db.connect()
    job = db.get_job(conn, args.job_id)
    if job is None:
        print("no such job")
        return 1
    if job["status"] != "queued":
        try:
            db.requeue(conn, args.job_id)
        except ValueError as err:
            print(err)
            return 1
    if not db.claim(conn, args.job_id, str(config.RUNS / str(args.job_id))):
        print("another process holds this job")
        return 1
    w = worker.Worker()
    w.run_one(profiles.load(job["profile"]), db.get_job(conn, args.job_id))
    job = db.get_job(conn, args.job_id)
    pause = db.pause_of(conn, job["profile"])
    if pause:
        print("rate limit: paused until", pause["until"], pause["reason"])
    status = job["status"]
    print(status, job.get("error") or "")
    if job.get("result") and db.result_of(job).get("files", {}).get("pdf"):
        print("PDF:", db.result_of(job)["files"]["pdf"])
    for a in db.result_of(job).get("attempts", []):
        print(f"  attempt {a['n']}: {a['duration_s']}s model={a.get('model')} cost={a.get('cost_usd')} "
              f"failures={a['failures']}")
    return 0


def cmd_recheck(args) -> int:
    """Re-run the 8 checks on stored attempt files (no Claude, no rendering): which failures the current checks
    still report. Default: the last attempt folder of each job."""
    import json
    from . import checks, docxio, pipeline
    conn = db.connect()
    engine = pipeline.engine_order(config.settings())[0]
    for job_id in args.job_ids:
        job = db.get_job(conn, job_id)
        if job is None:
            print(job_id, "no such job")
            continue
        run_dir = config.RUNS / str(job_id)
        folders = sorted((p for p in run_dir.glob("attempt*") if p.is_dir()), key=lambda p: int(p.name[7:]))
        folder = next((p for p in folders if args.attempt and p.name == f"attempt{args.attempt}"), None) \
            or (folders[-1] if folders else None)
        if folder is None or not (folder / "resume.pdf").exists():
            print(job_id, "no stored attempt with a PDF")
            continue
        prof = profiles.load(job["profile"])
        base = prof.base(job.get("base_id")) or prof.bases[0]
        measure = pipeline.measure_base(base, "word" if args.word_measure else engine, None)
        t = docxio.structure(docxio.read(folder / "resume.docx"))
        scores = pipeline._score(prof, job, t.full_text(), checks.era_texts(t, prof.id, prof.rules), base.families)
        inp = checks.Input(prof.id, base.structure, docxio.read(base.docx), measure, folder / "resume.docx",
                           folder / "resume.pdf", job["jd_text"], prof.known_skills, scores, base.id,
                           [b.text for b in prof.bases], prof.skill_text, prof.rules, job.get("role") or "",
                           job.get("location") or "", fact_bank=prof.fact_bank_resume)
        report = checks.run_all(inp)
        old = json.loads((folder / "checks.json").read_text()) if (folder / "checks.json").exists() else {}
        print(f"job {job_id} {folder.name}: stored {len(old.get('failures') or [])} failure(s) -> now "
              f"{len(report.failures)}")
        for f in old.get("failures") or []:
            print(f"   was: check {f['check']} {f['name']}: {f['message'][:140]}")
        for f in report.failures:
            print(f"   now: check {f.check} {f.name}: {f.message[:140]}")
        vis = report.info.get("visual") or {}
        print(f"   ink {vis.get('ink')} page box {vis.get('page_box')} protruding {len(vis.get('protruding') or [])}")
    return 0


def cmd_outdated(args) -> int:
    """the free re-check of every ready resume (old base, title rule, page count, date wraps)."""
    from . import outdated
    conn = db.connect()
    stale = 0
    for job in db.list_jobs(conn, statuses=["ready"], limit=1000):
        reasons = outdated.outdated(job, profiles.load(job["profile"]))
        stale += bool(reasons)
        print(f"{job['id']:>4} {job['profile']:<6} {job['company'][:28]:<28} "
              f"{'; '.join(reasons) if reasons else 'current'}")
    print(f"{stale} outdated of {len(db.list_jobs(conn, statuses=['ready'], limit=1000))} ready")
    return 0


def cmd_set_model(args) -> int:
    model = None if args.model in ("default", "") else args.model
    settings = config.save_settings({"model": model})
    print("model:", settings.get("model"))
    return 0


def cmd_token(args) -> int:
    from . import server
    print(f"http://127.0.0.1:{config.settings().get('port')}/?token={server.token()}")
    return 0


def cmd_serve(args) -> int:
    from . import server
    server.serve(lan=args.lan, port=args.port, with_worker=not args.no_worker)
    return 0


def cmd_demo(args) -> int:
    """A separate home (~/.resume-tailor-demo) so the demo never mixes with your own resumes, demo mode forced on,
    and the fictional Alex Rivera profile installed."""
    from . import server
    if not os.environ.get("RESUME_TAILOR_HOME"):
        config.set_home(Path.home() / ".resume-tailor-demo")
    os.environ["RESUME_TAILOR_DEMO"] = "1"
    config.ensure_dirs()
    profiles.install_example()
    print(f"Demo home: {config.DATA} (delete it any time)")
    server.serve(port=args.port)
    return 0


def cmd_worker(args) -> int:
    from . import worker
    worker.main(once=args.once)
    return 0


def _cpu_seconds(pid: str) -> float:
    txt = subprocess.run(["ps", "-o", "cputime=", "-p", pid], capture_output=True, text=True).stdout.strip()
    h, m, s = ([0, 0] + [float(x) for x in txt.replace("-", ":").split(":")])[-3:]
    return h * 3600 + m * 60 + s


def cmd_idle_cpu(args) -> int:
    """CPU seconds the worker process uses over --seconds while the queue is empty."""
    found = subprocess.run(["pgrep", "-f", "resume_tailor (worker|serve)"], capture_output=True, text=True).stdout.split()
    if not found:
        print("no worker running")
        return 1
    pid = found[0]
    before = _cpu_seconds(pid)
    time.sleep(args.seconds)
    used = _cpu_seconds(pid) - before
    print(f"worker pid {pid}: {used:.2f} CPU s over {args.seconds} s idle ({100 * used / args.seconds:.3f} %)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="resume-tailor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve"); p.add_argument("--lan", action="store_true"); p.add_argument("--port", type=int)
    p.add_argument("--no-worker", action="store_true"); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("demo"); p.add_argument("--port", type=int); p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("worker"); p.add_argument("--once", action="store_true"); p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("add"); p.add_argument("-p", "--profile", required=True); p.add_argument("-c", "--company", required=True)
    p.add_argument("-r", "--role", required=True); p.add_argument("--jd-file"); p.add_argument("--jd-url")
    p.add_argument("--base"); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("run"); p.add_argument("job_id", type=int); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("recheck"); p.add_argument("job_ids", type=int, nargs="+"); p.add_argument("--attempt", type=int)
    p.add_argument("--word-measure", action="store_true"); p.set_defaults(fn=cmd_recheck)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("outdated").set_defaults(fn=cmd_outdated)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("token").set_defaults(fn=cmd_token)
    p = sub.add_parser("set-model"); p.add_argument("model"); p.set_defaults(fn=cmd_set_model)
    p = sub.add_parser("idle-cpu"); p.add_argument("--seconds", type=int, default=120); p.set_defaults(fn=cmd_idle_cpu)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
