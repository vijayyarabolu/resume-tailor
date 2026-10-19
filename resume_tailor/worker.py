"""The queue worker. One thread per person, one job at a time per thread (settings `concurrency`). Idle = blocked
in select() on a Unix socket (the server pokes it when a job is added) plus a long timer, so idle CPU is near zero:
no busy polling. `serve` runs it in a background thread; `worker` runs it as its own process.

Rate limit from Claude -> that person's queue pauses until the reset time, the job goes back to queued, nothing is
dropped. Claude not reachable (no API key, Claude Code not logged in) -> the jobs wait with a message saying what to
fix and are re-checked every `login_recheck_min` minutes.
"""
import logging
import os
import select
import signal
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from . import claude_runner, config, db, gdocs, pipeline, profiles

log = logging.getLogger(__name__)
IDLE_TIMEOUT_S = 3600.0
RUNNING_TIMEOUT_S = 60.0


def wake() -> bool:
    """Poke a running worker (the server calls this after adding a job). False when no worker listens."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(config.SOCK_PATH))
            s.sendall(b"wake\n")
        return True
    except OSError:
        return False


def alive() -> bool:
    """A worker listens on the socket (a stale socket file from a crash does not count)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(config.SOCK_PATH))
        return True
    except OSError:
        return False


class Worker:
    def __init__(self, settings: dict | None = None, once: bool = False):
        self.settings = settings or config.settings()
        self.once = once
        self.conn = db.connect()
        self.google = gdocs.Google(self.conn, self.settings)
        self.threads: dict = {}
        self.logins: dict = {}            # pid -> (checked_at, status dict)
        self.stop = threading.Event()
        self.rfd, self.wfd = os.pipe()
        self.sock: socket.socket | None = None

    # ---------- accounts ----------

    def account(self, prof, force: bool = False) -> dict:
        checked_at, status = self.logins.get(prof.id, (0.0, None))
        recheck = 60.0 * float(self.settings.get("login_recheck_min") or 5)
        if force or status is None or time.monotonic() - checked_at > recheck:
            status = claude_runner.account(prof, self.settings)
            self.logins[prof.id] = (time.monotonic(), status)
            log.info("%s Claude: %s", prof.id, "ready" if status.get("logged_in") else status.get("error") or "not logged in")
        return status

    def login_text(self, prof) -> str:
        return claude_runner.login_text(prof, self.settings)

    def _park_for_login(self, prof) -> None:
        text = self.login_text(prof)
        for job in db.list_jobs(self.conn, prof.id, ("queued",)):
            db.set_status(self.conn, job["id"], "waiting", text, error=text)

    def _unpark(self, prof) -> None:
        for job in db.list_jobs(self.conn, prof.id, ("waiting",)):
            db.requeue(self.conn, job["id"], keep_attempts=True)

    # ---------- one job ----------

    def run_one(self, prof, job: dict) -> None:
        """One claimed job on its own SQLite connection (the dispatcher keeps self.conn)."""
        conn = None
        try:
            conn = db.connect()
            job = db.get_job(conn, job["id"])
            status = self.account(prof)
            db.update(conn, job["id"], account=status.get("email"))
            try:
                pipeline.process(conn, job, self.settings, self.google)
            except pipeline.Limit as err:
                until = err.until or (datetime.now(timezone.utc) + timedelta(
                    minutes=float(self.settings.get("pause_default_min") or 30)))
                db.set_pause(conn, prof.id, until, str(err)[:200])
                db.requeue(conn, job["id"], keep_attempts=True)
                db.event(conn, job["id"], prof.id, "toast",
                         f"{prof.name}: {claude_runner.pause_text(until)} (usage limit)", {"until": until.isoformat()})
            except pipeline.Login as err:
                self.logins.pop(prof.id, None)
                text = self.login_text(prof)
                db.set_status(conn, job["id"], "waiting", text, error=f"{text} ({err})")
                self._park_for_login(prof)
            except pipeline.Cancelled:
                db.set_status(conn, job["id"], "cancelled", "cancelled")
            except Exception as err:          # one job must never take the worker down
                log.exception("job %s failed", job["id"])
                db.set_status(conn, job["id"], "failed", "failed", error=f"{type(err).__name__}: {err}"[:900])
        except Exception:
            log.exception("job %s: could not record its outcome", job["id"])
        finally:
            if conn is not None:
                conn.close()
            try:
                os.write(self.wfd, b"x")
            except OSError:
                pass

    # ---------- the loop ----------

    def _listen(self) -> None:
        config.ensure_dirs()
        if config.SOCK_PATH.exists():
            config.SOCK_PATH.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(config.SOCK_PATH))
        os.chmod(config.SOCK_PATH, 0o600)
        self.sock.listen(8)
        self.sock.setblocking(False)

    def _drain(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except (BlockingIOError, OSError):
                break
            with client:
                client.setblocking(False)
                try:
                    client.recv(64)
                except OSError:
                    pass
        try:
            while os.read(self.rfd, 64):
                pass
        except BlockingIOError:
            pass

    def _recheck_s(self) -> float:
        return 60.0 * float(self.settings.get("login_recheck_min") or 5)

    def _dispatch_profile(self, pid: str, limit: int) -> tuple:
        """(busy, next_due_s) for one person."""
        try:
            prof = profiles.load(pid)
        except Exception as err:
            log.warning("profile %s unusable: %s", pid, err)
            return False, IDLE_TIMEOUT_S
        running = [t for t in self.threads.get(pid, []) if t.is_alive()]
        self.threads[pid] = running
        busy, due = bool(running), IDLE_TIMEOUT_S
        pause = db.pause_of(self.conn, pid)
        if pause:
            left = (db.parse_ts(pause["until"]) - datetime.now(timezone.utc)).total_seconds()
            return busy, max(1.0, left)
        if db.list_jobs(self.conn, pid, ("waiting",), limit=1):
            if self.account(prof).get("logged_in"):
                self._unpark(prof)
            else:
                due = min(due, self._recheck_s())
        while len(running) < limit:
            job = db.next_queued(self.conn, pid)
            if job is None:
                break
            if not self.account(prof).get("logged_in"):
                self._park_for_login(prof)
                due = min(due, self._recheck_s())
                break
            if not db.claim(self.conn, job["id"], str(config.RUNS / str(job["id"]))):
                continue
            t = threading.Thread(target=self.run_one, args=(prof, job), name=f"rg-{pid}-{job['id']}", daemon=True)
            t.start()
            running.append(t)
            busy = True
        return busy, due

    def _dispatch(self) -> tuple:
        """Start every job that can run now. Returns (busy, next_due_s)."""
        self.settings = config.settings()
        limit = int(self.settings.get("concurrency") or 1)
        busy, due = False, IDLE_TIMEOUT_S
        for pid in profiles.ids():
            b, d = self._dispatch_profile(pid, limit)
            busy, due = busy or b, min(due, d)
        return busy, due

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: (self.stop.set(), os.write(self.wfd, b"q")))

    def main(self) -> None:
        self._listen()
        os.set_blocking(self.rfd, False)
        self._install_signals()
        db.recover_stale(self.conn)
        log.info("worker up: socket %s, pid %s", config.SOCK_PATH, os.getpid())
        try:
            while not self.stop.is_set():
                try:
                    busy, due = self._dispatch()
                except Exception:                 # a bad settings file or profile must not kill the agent
                    log.exception("dispatch failed; retrying in %s s", RUNNING_TIMEOUT_S)
                    busy, due = True, RUNNING_TIMEOUT_S
                if self.once and not busy and all(db.next_queued(self.conn, p) is None for p in profiles.ids()):
                    break
                timeout = min(due, RUNNING_TIMEOUT_S if busy else IDLE_TIMEOUT_S)
                readable, _, _ = select.select([self.sock, self.rfd], [], [], timeout)
                if readable:
                    self._drain()
        finally:
            if self.sock:
                self.sock.close()
            if config.SOCK_PATH.exists():
                config.SOCK_PATH.unlink()


def main(once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Worker(once=once).main()
