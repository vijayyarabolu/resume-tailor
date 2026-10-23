"""A SQLite DB broke while a virtual machine with the folder shared into it held it open. SQLite's locks do not cross
such a share or a network file system, so on Linux the app refuses to open its DB from a shared or network mount
(RESUME_TAILOR_ALLOW_SHARED_DB=1 opens one there on purpose)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from resume_tailor import db


class SharedMountTest(unittest.TestCase):
    def test_the_mount_of_a_path_is_its_longest_mount_point(self):
        mounts = "/dev/vda1 / ext4 rw 0 0\nmac /mnt/mac virtiofs rw 0 0\n"
        self.assertEqual(db.mount_type("/mnt/mac/Documents/resume-tailor/resume-tailor.db", mounts), "virtiofs")
        self.assertEqual(db.mount_type("/mnt/macintosh/x.db", mounts), "ext4")

    def test_a_db_on_a_shared_mount_is_refused_unless_asked(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(db.sys, "platform", "linux"), \
                mock.patch.object(db, "_mounts", lambda: f"share {Path(tmp).resolve()} 9p rw 0 0\n"), \
                mock.patch.dict("os.environ", {}):
            os.environ.pop(db.ALLOW_SHARED, None)
            with self.assertRaises(db.SharedDatabase):
                db.connect(Path(tmp) / "resume-tailor.db")
            self.assertFalse((Path(tmp) / "resume-tailor.db").exists())
            os.environ[db.ALLOW_SHARED] = "1"
            db.connect(Path(tmp) / "resume-tailor.db").close()

    def test_a_mac_is_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(db.sys, "platform", "darwin"), \
                mock.patch.object(db, "_mounts", side_effect=AssertionError("read /proc/mounts on a Mac")):
            db.connect(Path(tmp) / "resume-tailor.db").close()


if __name__ == "__main__":
    unittest.main()
