"""Official start-time revisions must survive cached recovery and live polls."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from live_service import (
    COMPLETION_CHECK_INTERVAL, _completion_checks, _completion_updates,
)
from sync_service import BEIJING_TZ, apply_verified_tennis_snapshots, normalize_unit


class ScheduleRevisionTests(unittest.TestCase):
    day = "2026-09-28"
    key = "M.SINGLES-----------.R32-.001300--"

    def unit(self, hour=13, sport="TEN"):
        return {
            "Disc": sport, "Key": self.key, "Status": "SCHEDULED",
            "DateTimeRaw": f"{self.day}T{hour:02d}:00:00+09:00",
            "LocDesc": "Center Court", "Type": "T",
            "Home": {"Org": "JPN", "Name": "MATSUOKA Hayato"},
            "Away": {"Org": "HKG", "Name": "THOMPSON Kairan"},
        }

    def test_recovery_snapshot_cannot_revert_newer_time_court_or_matchup(self):
        row = normalize_unit(self.unit(), "TEN", self.day)
        row.update(court="Court 8", matchup="Updated home vs Updated away")
        before = deepcopy(row)
        for _ in range(2):  # Loading and restarting must both be idempotent.
            result = apply_verified_tennis_snapshots([row])
            row = next(item for item in result if item["id"] == before["id"])
            for field in ("scheduledAt", "officialScheduledAt", "time", "court", "matchup"):
                self.assertEqual(row[field], before[field], field)
        self.assertEqual(row["officialScheduledAt"], f"{self.day}T12:00:00+08:00")

    def test_prestart_daily_check_picks_up_delay_and_remains_throttled(self):
        now = datetime(2026, 9, 28, 8, tzinfo=BEIJING_TZ)
        for sport in ("TEN", "BDM"):
            with self.subTest(sport=sport), TemporaryDirectory() as directory:
                path = Path(directory) / "schedule.json"
                previous = [normalize_unit(self.unit(12, sport), sport, self.day)]
                target = [(sport, self.day)]
                _completion_checks.pop(str(path.resolve()), None)
                with patch("live_service.fetch_official_json", return_value=[self.unit(13, sport)]) as fetch:
                    updates = _completion_updates(path, previous, [], target, now)
                    self.assertEqual(len(updates), 1)
                    self.assertEqual(updates[0]["time"], "12:00")
                    self.assertEqual(updates[0]["officialScheduledAt"], f"{self.day}T12:00:00+08:00")
                    self.assertEqual(_completion_updates(path, previous, [], target, now + timedelta(seconds=5)), [])
                    self.assertEqual(fetch.call_count, 1)
                    _completion_updates(path, previous, [], target, now + timedelta(seconds=COMPLETION_CHECK_INTERVAL))
                    self.assertEqual(fetch.call_count, 2)
                _completion_checks.pop(str(path.resolve()), None)

    def test_failed_prestart_check_does_not_retry_every_score_poll(self):
        now = datetime(2026, 9, 28, 8, tzinfo=BEIJING_TZ)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            previous = [normalize_unit(self.unit(), "TEN", self.day)]
            with patch("live_service.fetch_official_json", side_effect=OSError("temporarily unavailable")) as fetch:
                for seconds in (0, 5, 10, COMPLETION_CHECK_INTERVAL - 1):
                    self.assertEqual(_completion_updates(path, previous, [], [("TEN", self.day)], now + timedelta(seconds=seconds)), [])
                self.assertEqual(fetch.call_count, 1)
            _completion_checks.pop(str(path.resolve()), None)


if __name__ == "__main__":
    unittest.main()
