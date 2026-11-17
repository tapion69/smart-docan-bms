"""Unit tests of missing-BMS scan cadence and reconnect backoff."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from recovery import missing_slots, scan_interval, retry_delay, is_attempt_due


class RecoveryTests(unittest.TestCase):
    def test_missing_slot_remains_reserved_when_only_three_seen(self):
        configured = [(1, "AA:BB:CC:DD:EE:01"),
                      (2, "AA:BB:CC:DD:EE:02"),
                      (3, "AA:BB:CC:DD:EE:03"),
                      (4, "AA:BB:CC:DD:EE:04")]
        seen = {configured[i][1] for i in (1, 2, 3)}
        self.assertEqual(missing_slots(configured, seen), [1])
        self.assertEqual(scan_interval(180, 30, bool(missing_slots(configured, seen))), 30)
        self.assertEqual(scan_interval(180, 30, False), 180)
        self.assertEqual(missing_slots(configured, {address for _, address in configured}), [])

    def test_only_failed_bms_has_bounded_retry_delay(self):
        self.assertEqual([retry_delay(n) for n in range(7)], [0, 5, 10, 20, 40, 60, 60])
        self.assertTrue(is_attempt_due(0, None, 0))
        self.assertTrue(is_attempt_due(0, 1.0, 2.0))
        self.assertFalse(is_attempt_due(2, 100.0, 109.0))
        self.assertTrue(is_attempt_due(2, 100.0, 110.0))
        self.assertFalse(is_attempt_due(3, 100.0, 119.0))
        self.assertTrue(is_attempt_due(3, 100.0, 120.0))

    def test_unavailable_battery_does_not_shorten_scans_when_none_configured(self):
        self.assertEqual(missing_slots([], set()), [])
        self.assertEqual(scan_interval(180, 30, False), 180)


if __name__ == "__main__":
    unittest.main()
