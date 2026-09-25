import unittest

from sync_service import normalize_unit


class NotBeforeTimezoneTests(unittest.TestCase):
    def unit(self, **updates):
        return {
            "Key": "mixed-doubles-test",
            "DateTimeRaw": "2026-09-25T12:50:00+09:00",
            "Phase": "X.DOUBLES-----------.8FNL",
            **updates,
        }

    def test_not_before_converts_japan_time_to_beijing(self):
        for field in ("NotBefore", "NotBeforeRaw", "NotBeforeTime"):
            for timestamp in ("2026-09-25T16:00:00", "2026-09-25T16:00:00+09:00", "2026-09-25T15:00:00+08:00"):
                with self.subTest(field=field, timestamp=timestamp):
                    row = normalize_unit(self.unit(**{field: timestamp}), "BDM")
                    self.assertEqual(row["scheduledAt"], "2026-09-25T15:00:00+08:00")

    def test_midnight_not_before_uses_previous_beijing_date(self):
        row = normalize_unit(self.unit(NotBefore="2026-09-26T00:30:00"), "BDM")
        self.assertEqual(row["date"], "2026-09-25")
        self.assertEqual(row["time"], "23:30")
        self.assertEqual(row["sourceDate"], "2026-09-26")

    def test_explicit_reschedule_is_not_overwritten_by_session_fallback(self):
        for field in ("NotBefore", "NotBeforeRaw", "NotBeforeTime", "StartTime"):
            with self.subTest(field=field):
                row = normalize_unit(self.unit(**{field: "2026-09-25T17:30:00+09:00"}), "BDM")
                self.assertEqual(row["time"], "16:30")

    def test_known_session_fallback_remains_without_explicit_update(self):
        self.assertEqual(normalize_unit(self.unit(), "BDM")["time"], "15:00")

    def test_normal_offset_timestamp_remains_unchanged(self):
        row = normalize_unit(self.unit(Phase="W.SINGLES", DateTimeRaw="2026-09-25T16:00:00+09:00"), "BDM")
        self.assertEqual(row["scheduledAt"], "2026-09-25T15:00:00+08:00")


if __name__ == "__main__":
    unittest.main()

