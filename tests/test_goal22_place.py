"""A request is never refused for its location. "Brooklyn Park,MN" is tidied, an address is read for its city,
and a place that still can't be worked out waits in Queue as "needs you: pick the location" with its cities; the
person's pick queues it."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from resume_tailor import config, db, places, profiles, server

from tests.helpers import JD_TEXT, TempEnv

ADDRESS = "4100 Harbor Pkwy N,Suite 210 Brooklyn Park,MN 55445"     # a made-up street address


def item(**extra):
    return {"profile": "alex-rivera", "company": "Acme", "role": "Data Engineer, Finance", "jd_text": JD_TEXT,
            "workplace": "onsite", **extra}


class Normalise(unittest.TestCase):
    def test_city_st_and_loose_spacing_need_no_address_reading(self):
        with mock.patch.object(places, "city", side_effect=AssertionError("no address reading")):
            for text, want in (("Wilmington, MA", "Wilmington, MA"), ("Brooklyn Park,MN", "Brooklyn Park, MN"),
                               (" austin , tx ", "Austin, TX"), ("brooklyn park ,mn", "Brooklyn Park, MN")):
                with self.subTest(text=text):
                    self.assertEqual(places.normalise(text)[0], want)

    def test_an_address_is_read_for_its_city(self):
        with mock.patch.object(places, "city", return_value={"location": "Brooklyn Park, MN",
                                                               "choices": ["Brooklyn Park, MN"]}) as city:
            self.assertEqual(places.normalise(ADDRESS), ("Brooklyn Park, MN", ["Brooklyn Park, MN"]))
        city.assert_called_once_with(ADDRESS)

    def test_unclear_gives_none(self):
        with mock.patch.object(places, "city", return_value={"location": None, "choices": ["Tarrant, AL", "bad"]}):
            self.assertEqual(places.normalise("Birmingham area"), (None, ["Tarrant, AL"]))
        self.assertEqual(places.normalise("somewhere"), (None, []))

    def test_several_cities_are_never_taken_as_the_first(self):
        with mock.patch.object(places, "city", return_value={"location": "New York, NY",
                                                               "choices": ["New York, NY", "Columbus, OH"]}):
            self.assertEqual(places.normalise("New York, NY; Columbus, OH"), (None, ["New York, NY", "Columbus, OH"]))

    def test_pick(self):
        self.assertEqual(places.pick("Remote"), ("remote", None))
        self.assertEqual(places.pick("Tarrant, AL"), ("", "Tarrant, AL"))
        with mock.patch.object(places, "city", return_value={"location": None, "choices": []}):
            with self.assertRaisesRegex(ValueError, "Brooklyn Park, MN"):
                places.pick("somewhere nice")


class NeverRefused(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.conn = db.connect()
        self.known = set(profiles.ids())

    def tearDown(self):
        self.conn.close()
        self.env.__exit__(None, None, None)

    def add(self, **extra):
        return db.get_job(self.conn, db.add_job(self.conn, *server.Handler._validate_job(None, item(**extra), self.known)))

    def test_an_address_is_queued_with_its_city(self):
        with mock.patch.object(places, "city", return_value={"location": "Brooklyn Park, MN", "choices": []}):
            job = self.add(location=ADDRESS)
        self.assertEqual((job["status"], job["location"], job["error"]), ("queued", "Brooklyn Park, MN", None))

    def test_an_unclear_place_waits_for_the_person_and_the_worker_skips_it(self):
        with mock.patch.object(places, "city", return_value={"location": None, "choices": ["Tarrant, AL"]}):
            job = self.add(location="Birmingham area; Tarrant, AL 35217")
        self.assertEqual((job["status"], job["location"], job["error"]), ("needs_place", None, places.NEEDS))
        self.assertEqual(json.loads(job["place_choices"]), ["Tarrant, AL"])
        self.assertIsNone(db.next_queued(self.conn, "alex-rivera"))
        view = server._job_view(job)
        self.assertEqual((view["place_choices"], view["home"]), (["Tarrant, AL"], "Portland, OR"))
        db.set_place(self.conn, job["id"], "onsite", "Tarrant, AL")
        self.assertEqual(db.next_queued(self.conn, "alex-rivera")["location"], "Tarrant, AL")
        with self.assertRaises(ValueError):                                   # picked once
            db.set_place(self.conn, job["id"], "onsite", "Austin, TX")

    def test_a_waiting_pick_can_be_cancelled_but_not_archived(self):
        with mock.patch.object(places, "city", return_value={"location": None, "choices": []}):
            job = self.add(location="??")
        with self.assertRaises(ValueError):
            db.archive(self.conn, job["id"])
        self.assertEqual(db.cancel(self.conn, job["id"]), "cancelled")

    def test_retry_cannot_skip_the_pick(self):
        with mock.patch.object(places, "city", return_value={"location": None, "choices": []}):
            job = self.add(location="??")
        with self.assertRaisesRegex(ValueError, "location picked first"):
            server.Handler._action(mock.Mock(app=mock.Mock(conn=self.conn)), job["id"], "retry", {})


class PlaceAction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv().__enter__()
        cls.app = server.App()
        cls.httpd = server.make_server(cls.app, 0, "127.0.0.1")
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = config.TOKEN_PATH.read_text().strip()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.__exit__(None, None, None)

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_post_jobs_is_201_and_the_pick_queues_it(self):
        with mock.patch.object(places, "city", return_value={"location": None, "choices": ["Tarrant, AL"]}), \
                mock.patch.object(server.worker, "wake", return_value=False):
            status, out = self.post("/api/jobs", {"jobs": [item(location="Birmingham area; Tarrant, AL"),
                                                           item(location="Austin, TX")]})
            self.assertEqual(status, 201)                                    # both added, nothing refused
            waiting, fine = (db.get_job(self.app.conn, i) for i in out["ids"])
            self.assertEqual((waiting["status"], fine["status"]), ("needs_place", "queued"))
            self.assertEqual(self.post(f"/api/jobs/{waiting['id']}/place", {"location": "nowhere"})[0], 400)
            status, out = self.post(f"/api/jobs/{waiting['id']}/place", {"location": "remote"})
        self.assertEqual((status, out["status"], out["location"]), (200, "queued", None))
        job = db.get_job(self.app.conn, waiting["id"])
        self.assertEqual((job["status"], job["workplace"], job["location"]), ("queued", "remote", None))


if __name__ == "__main__":
    unittest.main()
