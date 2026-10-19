"""(user 23:19): archived resumes deleted for good. One pressed (× Delete for good), all of a person's (Clear
archived) or the ones archived more than `archived_delete_days` ago (once a day). Gone: the row and its events, its
local run files (data/runs/<id>/, earlier versions included) and its "Open as Google Doc" copy. Never the approved
Drive PDF: the person's Sheet links it (applied resumes stay)."""
import logging
import shutil

from . import config, db

log = logging.getLogger(__name__)
GRACE_S = 15            # the Undo toast shows 8 s; the files go only after it


def _one(conn, job: dict, delete_doc) -> None:
    if job.get("gdoc_id"):
        try:
            delete_doc(job["profile"], job["gdoc_id"])
        except Exception as err:                 # a Doc already gone or a token refused must not keep the row
            log.warning("job %s: its Google Doc copy could not be deleted: %s", job["id"], err)
    run = (config.RUNS / str(int(job["id"]))).resolve()
    if run.parent == config.RUNS.resolve() and run.is_dir():
        shutil.rmtree(run, ignore_errors=True)
    db.purge_row(conn, job["id"])
    log.info("job %s (%s · %s) deleted for good", job["id"], job["company"], job["role"])


def due(conn, delete_doc, grace_s: float = GRACE_S, only=None) -> list:
    """Delete the resumes marked longer ago than the grace (`only`: just these ids); returns their ids.
    delete_doc(profile, doc_id)."""
    done = []
    for job in db.due_deletes(conn, grace_s):
        if only is not None and job["id"] not in only:
            continue
        _one(conn, job, delete_doc)
        done.append(job["id"])
    return done


def auto(conn, days: int, delete_doc) -> list:
    """Archived more than `days` ago -> deleted now (0 = never)."""
    if not days:
        return []
    ids = db.mark_delete(conn, db.old_archived(conn, int(days)))
    return due(conn, delete_doc, grace_s=0, only=set(ids))       # a × Delete pressed just now keeps its Undo time
