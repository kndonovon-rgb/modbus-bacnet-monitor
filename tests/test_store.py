"""The store, with attention to the distinction between no data and bad data."""
import os
import tempfile
import time
import unittest

from dcmon.store import Store


class Basics(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dcmon-test-")
        self.s = Store(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.s.close()

    def test_round_trip(self):
        self.s.record("p", 12.5, ts=100.0)
        row = self.s.latest("p")
        self.assertAlmostEqual(row["value"], 12.5)
        self.assertEqual(row["ok"], 1)

    def test_creates_its_parent_directory(self):
        nested = os.path.join(self.dir, "a", "b", "t.db")
        s = Store(nested)
        s.record("p", 1.0)
        s.close()
        self.assertTrue(os.path.exists(nested))

    def test_latest_of_an_unseen_point_is_none_not_zero(self):
        self.assertIsNone(self.s.latest("never-polled"))

    def test_series_is_returned_oldest_first(self):
        for i in range(5):
            self.s.record("p", float(i), ts=100.0 + i)
        values = [r["value"] for r in self.s.series("p")]
        self.assertEqual(values, [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_series_respects_a_window(self):
        for i in range(10):
            self.s.record("p", float(i), ts=100.0 + i)
        self.assertEqual(len(self.s.series("p", since=105.0)), 5)

    def test_points_are_listed(self):
        self.s.record("b", 1.0)
        self.s.record("a", 1.0)
        self.assertEqual(self.s.points(), ["a", "b"])


class LatestOk(unittest.TestCase):
    """Why staleness must be measured from the last *successful* reading.

    A dead device still produces a row every cycle, because the monitor keeps
    asking and keeps recording the failure. Age measured from the newest row
    of any kind therefore stays near zero forever, and the point reads as
    perfectly current while it has not been measured since yesterday.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dcmon-test-")
        self.s = Store(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.s.close()

    def test_latest_ok_skips_failed_polls(self):
        self.s.record("p", 55.0, ts=1000.0, ok=True)
        for i in range(1, 60):
            self.s.record("p", None, ts=1000.0 + i, ok=False, detail="no answer")
        self.assertAlmostEqual(self.s.latest("p")["ts"], 1059.0)
        self.assertAlmostEqual(self.s.latest_ok("p")["ts"], 1000.0)

    def test_the_difference_is_the_whole_point(self):
        self.s.record("p", 55.0, ts=1000.0, ok=True)
        for i in range(1, 60):
            self.s.record("p", None, ts=1000.0 + i, ok=False)
        now = 1059.0
        naive_age = now - self.s.latest("p")["ts"]
        honest_age = now - self.s.latest_ok("p")["ts"]
        self.assertAlmostEqual(naive_age, 0.0)
        self.assertAlmostEqual(honest_age, 59.0)

    def test_latest_ok_is_none_when_nothing_ever_succeeded(self):
        self.s.record("p", None, ts=1.0, ok=False)
        self.assertIsNotNone(self.s.latest("p"))
        self.assertIsNone(self.s.latest_ok("p"))


class Summaries(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dcmon-test-")
        self.s = Store(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.s.close()

    def test_failed_polls_are_counted_but_not_averaged(self):
        """A mean that quietly averages only the readings that arrived hides
        the interval worth looking at."""
        for v in (10.0, 20.0, 30.0):
            self.s.record("p", v, ts=100.0, ok=True)
        for _ in range(7):
            self.s.record("p", None, ts=100.0, ok=False)
        s = self.s.summary("p", since=0.0)
        self.assertEqual(s["n"], 10)
        self.assertEqual(s["failed"], 7)
        self.assertAlmostEqual(s["mean"], 20.0)
        self.assertAlmostEqual(s["lo"], 10.0)
        self.assertAlmostEqual(s["hi"], 30.0)

    def test_an_all_failed_window_reports_no_statistics_rather_than_zero(self):
        for _ in range(4):
            self.s.record("p", None, ts=100.0, ok=False)
        s = self.s.summary("p", since=0.0)
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["failed"], 4)
        self.assertIsNone(s["mean"])
        self.assertIsNone(s["lo"])


class Events(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dcmon-test-")
        self.s = Store(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.s.close()

    def test_events_are_separate_from_readings(self):
        """So a threshold change never rewrites the measurement history."""
        self.s.record("p", 99.0, ts=100.0)
        self.s.record_event("p", "ALARM", value=99.0, ts=100.0, detail="high")
        self.assertEqual(len(self.s.series("p")), 1)
        self.assertEqual(len(self.s.recent_events()), 1)
        self.assertEqual(self.s.recent_events()[0]["state"], "ALARM")

    def test_pruning_readings_leaves_events(self):
        self.s.record("p", 1.0, ts=100.0)
        self.s.record_event("p", "ALARM", ts=100.0)
        removed = self.s.prune(before=200.0)
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.s.recent_events()), 1)


if __name__ == "__main__":
    unittest.main()
