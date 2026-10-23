"""A tracker asks for every resume (`/api/jobs?lean=1`) every few seconds from each open page. Live, on a Mac short
of memory, one answer took 7 to 26 s (0.1 s on a copy of the same data), and the tracker gave up after 4 s and
showed the app as offline. A resume's lean view is now built once per change of its row (`rev`, bumped by a
trigger on every UPDATE) and the freshness check reads a covering index, never the JD or result columns. A page that
hangs up early is one log line, not two tracebacks."""
import io
import json
import logging
import unittest
from contextlib import redirect_stderr
from unittest import mock

from resume_tailor import db, server
from tests import test_goal5_edits as g5


class LeanCacheTests(unittest.TestCase):
    tearDown, call, _made = g5.ApiTests.tearDown, g5.ApiTests.call, g5.ApiTests._made

    def setUp(self):
        g5.ApiTests.setUp(self)
        server._LEAN.clear()

    def _views_built(self, path="/api/jobs?profile=alex-rivera&lean=1"):
        real = server._job_view
        with mock.patch.object(server, "_job_view", side_effect=real) as spy:
            _, out = self.call(path)
        return spy.call_count, out

    def test_unchanged_resumes_are_not_rebuilt_on_the_next_ask(self):
        ids = [self._made() for _ in range(5)]
        built, first = self._views_built()
        self.assertEqual(built, 5)
        built, again = self._views_built()
        self.assertEqual(built, 0)
        self.assertEqual(again, first)
        self.assertEqual([j["id"] for j in again["jobs"]], sorted(ids, reverse=True))

    def test_any_change_to_a_row_rebuilds_only_that_resume(self):
        a, b = self._made(), self._made()
        self._views_built()
        db.update(self.conn, a, error="changed")
        built, out = self._views_built()
        self.assertEqual(built, 1)
        self.assertEqual(next(j for j in out["jobs"] if j["id"] == a)["error"], "changed")
        self.conn.execute("UPDATE jobs SET downloaded_at = '2026-10-20T10:00:00Z' WHERE id = ?", (b,))   # no updated_at
        built, out = self._views_built()
        self.assertEqual(built, 1)
        self.assertEqual(next(j for j in out["jobs"] if j["id"] == b)["times"]["downloaded"], "2026-10-20T10:00:00Z")

    def test_cached_answer_equals_a_fresh_build(self):
        for status in ("ready", "needs_you", "ready"):
            self._made(status)
        self._views_built()
        _, cached = self.call("/api/jobs?profile=alex-rivera&lean=1")
        fresh = [server._lean(server._job_view(j)) for j in db.list_jobs(self.conn, "alex-rivera", limit=server.LEAN_LIMIT)]
        self.assertEqual(cached["jobs"], json.loads(json.dumps(fresh)))

    def test_each_person_sees_only_their_own_and_deleted_ones_leave(self):
        mine, hers = self._made(), self._made(profile="sam-lee")
        _, out = self.call("/api/jobs?profile=sam-lee&lean=1")
        self.assertEqual([j["id"] for j in out["jobs"]], [hers])
        self.conn.execute("UPDATE jobs SET deleting_at = '2026-10-20T10:00:00Z' WHERE id = ?", (mine,))
        _, out = self.call("/api/jobs?profile=alex-rivera&lean=1")
        self.assertEqual(out["jobs"], [])

    def test_a_resume_that_leaves_the_list_leaves_the_cache(self):
        # review: views of deleted or purged resumes were kept for the server's life
        mine, other, hers = self._made(), self._made(), self._made(profile="sam-lee")
        self.call("/api/jobs?profile=alex-rivera&lean=1")
        self.call("/api/jobs?profile=sam-lee&lean=1")
        self.conn.execute("UPDATE jobs SET deleting_at = '2026-10-20T10:00:00Z' WHERE id = ?", (mine,))
        self.call("/api/jobs?profile=alex-rivera&lean=1")
        self.assertEqual(sorted(server._LEAN), sorted([other, hers]))

    def test_the_freshness_check_reads_only_an_index(self):
        plan = " ".join(r[3] for r in self.conn.execute("EXPLAIN QUERY PLAN " + db.REVS_SQL, ("alex-rivera", 10)))
        self.assertIn("COVERING INDEX", plan)

    def test_every_update_bumps_rev(self):
        jid = self._made()
        rev = self.conn.execute("SELECT rev FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
        self.conn.execute("UPDATE jobs SET downloaded_at = 'x' WHERE id = ?", (jid,))
        self.assertEqual(self.conn.execute("SELECT rev FROM jobs WHERE id = ?", (jid,)).fetchone()[0], rev + 1)


class HangUpTests(unittest.TestCase):
    def test_a_page_that_hangs_up_is_one_line_and_no_500_attempt(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/jobs?profile=alex-rivera&lean=1"
        with mock.patch.object(handler, "_get", side_effect=BrokenPipeError), \
                mock.patch.object(handler, "_error") as error, self.assertLogs("resume_tailor.server", logging.INFO) as logs:
            handler.do_GET()
        error.assert_not_called()
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("Traceback", logs.output[0])

    def test_answered_polls_leave_no_log_line_but_failures_and_actions_do(self):
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 1)
        with self.assertLogs("resume_tailor.server", logging.INFO) as logs:
            handler.log_message('"%s" %s %s', "GET /api/jobs?profile=alex-rivera&lean=1 HTTP/1.1", "200", "-")
            handler.log_message('"%s" %s %s', "GET /api/state?profile=alex-rivera HTTP/1.1", "200", "-")
            handler.log_message('"%s" %s %s', "GET /api/jobs?profile=alex-rivera&lean=1 HTTP/1.1", "500", "-")
            handler.log_message('"%s" %s %s', "POST /api/jobs HTTP/1.1", "201", "-")
        self.assertEqual(len(logs.output), 2)
        self.assertIn("500", logs.output[0])

    def test_a_reset_between_requests_prints_no_traceback(self):      # and no INFO line either: a closed tab
        err = io.StringIO()
        with redirect_stderr(err), self.assertNoLogs("resume_tailor.server", logging.INFO):
            try:
                raise ConnectionResetError(54, "Connection reset by peer")
            except ConnectionResetError:
                server.QuietServer.handle_error(mock.Mock(), None, ("127.0.0.1", 53690))
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
