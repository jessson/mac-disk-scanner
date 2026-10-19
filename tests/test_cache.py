import tempfile
import time
import unittest
from pathlib import Path

from mac_disk_scanner.cache import ScanCache
from mac_disk_scanner.models import ScanNode


def node(name: str) -> ScanNode:
    return ScanNode(path=f"/x/{name}", name=name, category="Directory")


class SaveRegionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = ScanCache(Path(self.tmp.name) / "scan-cache.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resave_keeps_original_scan_time(self):
        scanned_at = time.time() - 3 * 24 * 3600
        self.cache.save_region("home", [node("old")], created_at=scanned_at)

        # Persisting an in-place edit (delete, lazy expand, sizing) must not
        # make a three-day-old scan look fresh.
        self.cache.save_region("home", [node("edited")])

        _, meta = self.cache.load_region("home", allow_stale=True)
        self.assertAlmostEqual(meta["created_at"], scanned_at, places=3)
        self.assertTrue(meta["stale"])

    def test_first_save_without_scan_time_uses_now(self):
        before = time.time()
        self.cache.save_region("temporary", [node("a")])
        _, meta = self.cache.load_region("temporary")
        self.assertGreaterEqual(meta["created_at"], before)

    def test_new_scan_replaces_scan_time(self):
        self.cache.save_region("caches", [node("a")], created_at=1.0)
        now = time.time()
        self.cache.save_region("caches", [node("b")], created_at=now)
        roots, meta = self.cache.load_region("caches")
        self.assertEqual(roots[0].name, "b")
        self.assertAlmostEqual(meta["created_at"], now, places=3)


if __name__ == "__main__":
    unittest.main()
