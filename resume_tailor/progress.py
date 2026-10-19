"""(user 22:21): what a running Re-import is doing right now ("reading your Doc", "building the PDF",
"checking"), in this server process only, so both apps' busy lines can say it; the step times go into the result."""
import threading
import time

_lock = threading.Lock()
_steps: dict[int, list] = {}                   # job id -> [[step, started (epoch s)], ...]


def step(job_id: int, text: str) -> None:
    with _lock:
        _steps.setdefault(job_id, []).append([text, time.time()])


def get(job_id: int) -> dict | None:
    """{"step", "since" (the run's start), "at" (this step's start)} while it runs, else None."""
    with _lock:
        done = _steps.get(job_id)
        return {"step": done[-1][0], "since": done[0][1], "at": done[-1][1]} if done else None


def timings(job_id: int) -> list:
    """[[step, seconds], ...] so far (the last one up to now)."""
    with _lock:
        done = list(_steps.get(job_id) or [])
    ends = [t for _, t in done[1:]] + [time.time()]
    return [[name, round(end - start, 1)] for (name, start), end in zip(done, ends)]


def clear(job_id: int) -> None:
    with _lock:
        _steps.pop(job_id, None)
