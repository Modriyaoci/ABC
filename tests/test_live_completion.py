import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from live_service import LIVE_NOW_PATH, _completion_checks, sync_live
from sync_service import BEIJING_TZ, SyncError, normalize_unit


class LiveCompletionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "schedule.json"
        self.now = datetime(2026, 9, 21, 20, tzinfo=BEIJING_TZ)
        _completion_checks.clear()

    def unit(self, key="match", sport="TTE", status="RUNNING", hour=19, scores=(2, 1)):
        return {
            "Disc": sport, "Key": key, "Status": status,
            "DateTimeRaw": f"2026-09-21T{hour:02d}:00:00+08:00", "Type": "T",
            "Home": {"Org": "CHN", "Result": scores[0]},
            "Away": {"Org": "JPN", "Result": scores[1]},
        }

    def save(self, *units):
        records = [normalize_unit(unit, unit["Disc"]) for unit in units]
        payload = {"meta": {"generatedAt": "original", "officialDays": {
            row["sport"]: ["2026-09-21"] for row in records
        }}, "records": records}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_missing_live_match_gets_confirmed_final_without_losing_future_fixture(self):
        future = self.unit("future", status="SCHEDULED", hour=22, scores=(None, None))
        self.save(self.unit(), future)
        final = self.unit(status="OFFICIAL", scores=(3, 1))
        with patch("live_service.fetch_official_json", side_effect=[[], [final]]) as fetch:
            result = sync_live(self.path, self.now)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args, ("/s/AG2026/en/TTE/schedule/daily/2026-09-21", 1))
        self.assertEqual([(row["status"], row["score"]) for row in result["records"]],
                         [("OFFICIAL", "3 : 1"), ("SCHEDULED", "待赛")])
        self.assertFalse(result["records"][0]["isLive"])

    def test_empty_daily_response_keeps_bytes_and_checks_only_once_per_minute(self):
        self.save(self.unit())
        original = self.path.read_bytes()
        for seconds, expected_calls in ((0, 2), (5, 1), (59, 1), (60, 2)):
            with self.subTest(seconds=seconds), patch("live_service.fetch_official_json", return_value=[]) as fetch:
                sync_live(self.path, self.now + timedelta(seconds=seconds))
            self.assertEqual(fetch.call_count, expected_calls)
            self.assertEqual(self.path.read_bytes(), original)

    def test_failed_final_lookup_does_not_block_current_live_score_or_repeat_immediately(self):
        self.save(self.unit(), self.unit("active", sport="BDM"))
        active = self.unit("active", sport="BDM", scores=(2, 2))
        with patch("live_service.fetch_official_json", side_effect=[[active], SyncError("offline")]):
            result = sync_live(self.path, self.now)
        rows = {row["id"]: row for row in result["records"]}
        self.assertEqual(rows["BDM:active"]["score"], "2 : 2")
        self.assertEqual(rows["TTE:match"]["status"], "RUNNING")
        with patch("live_service.fetch_official_json", return_value=[active]) as fetch:
            sync_live(self.path, self.now + timedelta(seconds=5))
        self.assertEqual(fetch.call_count, 1)

    def test_current_live_feed_wins_when_daily_response_also_contains_that_match(self):
        self.save(self.unit("active"), self.unit("finished"))
        active = self.unit("active", scores=(2, 2))
        daily = [self.unit("active", scores=(1, 0)), self.unit("finished", status="OFFICIAL", scores=(3, 0))]
        with patch("live_service.fetch_official_json", side_effect=[[active], daily]):
            result = sync_live(self.path, self.now)
        rows = {row["id"]: row for row in result["records"]}
        self.assertEqual(rows["TTE:active"]["score"], "2 : 2")
        self.assertEqual(rows["TTE:finished"]["status"], "OFFICIAL")

    def test_daily_check_adds_new_fixtures_and_applies_published_time_changes(self):
        self.save(self.unit(), self.unit("later", status="SCHEDULED", hour=21, scores=(None, None)))
        daily = [self.unit(status="OFFICIAL", scores=(3, 0)),
                 self.unit("later", status="SCHEDULED", hour=22, scores=(None, None)),
                 self.unit("new", status="START_LIST", hour=21, scores=(None, None))]
        with patch("live_service.fetch_official_json", side_effect=[[], daily]):
            result = sync_live(self.path, self.now)
        rows = {row["id"]: row for row in result["records"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["TTE:later"]["time"], "22:00")
        self.assertEqual(rows["TTE:new"]["status"], "START_LIST")

    def test_match_never_seen_in_live_feed_can_still_receive_final_result(self):
        self.save(self.unit(status="SCHEDULED", scores=(None, None)))
        with patch("live_service.fetch_official_json", side_effect=[[], [self.unit(status="OFFICIAL", scores=(3, 0))]]):
            result = sync_live(self.path, self.now)
        self.assertEqual(result["records"][0]["status"], "OFFICIAL")

    def test_incomplete_final_and_missing_rows_do_not_erase_existing_scores(self):
        self.save(self.unit(), self.unit("missing"))
        original = self.path.read_bytes()
        daily = [self.unit(status="OFFICIAL", scores=(None, None)), {}, {"Key": "broken", "Status": "OFFICIAL"}]
        with patch("live_service.fetch_official_json", side_effect=[[], daily]):
            sync_live(self.path, self.now)
        self.assertEqual(self.path.read_bytes(), original)

    def test_temporary_prestart_row_cannot_reset_running_score(self):
        self.save(self.unit())
        with patch("live_service.fetch_official_json", side_effect=[[], [self.unit(status="START_LIST", scores=(None, None))]]):
            result = sync_live(self.path, self.now)
        self.assertEqual(result["records"][0]["status"], "RUNNING")
        self.assertEqual(result["records"][0]["score"], "2 : 1")

    def test_cancelled_match_does_not_require_a_score(self):
        self.save(self.unit(status="SCHEDULED", scores=(None, None)))
        with patch("live_service.fetch_official_json", side_effect=[[], [self.unit(status="CANCELED", scores=(None, None))]]):
            result = sync_live(self.path, self.now)
        self.assertEqual(result["records"][0]["status"], "CANCELED")

    def test_unofficial_score_keeps_being_checked_for_confirmation(self):
        self.save(self.unit())
        with patch("live_service.fetch_official_json", side_effect=[[], [self.unit(status="UNOFFICIAL", scores=(3, 0))]]):
            result = sync_live(self.path, self.now)
        self.assertEqual(result["records"][0]["status"], "UNOFFICIAL")
        with patch("live_service.fetch_official_json", side_effect=[[], [self.unit(status="OFFICIAL", scores=(3, 0))]]):
            result = sync_live(self.path, self.now + timedelta(seconds=60))
        self.assertEqual(result["records"][0]["status"], "OFFICIAL")

    def test_future_and_confirmed_finished_fixtures_need_no_additional_request(self):
        self.save(self.unit(status="OFFICIAL"), self.unit("future", status="SCHEDULED", hour=22))
        with patch("live_service.fetch_official_json", return_value=[]) as fetch:
            sync_live(self.path, self.now)
        self.assertEqual(fetch.call_args.args, (LIVE_NOW_PATH, 1))
        self.assertEqual(fetch.call_count, 1)

    def test_only_one_daily_target_per_cycle_and_fair_rotation(self):
        self.save(self.unit(sport="BDM"), self.unit(sport="TEN"), self.unit(sport="TTE"))
        checked = []
        for seconds in (0, 5, 10):
            with patch("live_service.fetch_official_json", return_value=[]) as fetch:
                sync_live(self.path, self.now + timedelta(seconds=seconds))
            self.assertEqual(fetch.call_count, 2)
            checked.append(fetch.call_args.args[0])
        self.assertEqual(len(set(checked)), 3)
        with patch("live_service.fetch_official_json", return_value=[]) as fetch:
            sync_live(self.path, self.now + timedelta(seconds=15))
        self.assertEqual(fetch.call_count, 1)

    def test_known_matchup_survives_incomplete_team_names_in_confirmed_result(self):
        original = self.save(self.unit())
        final = self.unit(status="OFFICIAL", scores=(3, 1))
        final["Home"].pop("Org")
        final["Away"].pop("Org")
        with patch("live_service.fetch_official_json", side_effect=[[], [final]]):
            result = sync_live(self.path, self.now)
        self.assertEqual(result["records"][0]["matchup"], original["records"][0]["matchup"])
        self.assertEqual(result["records"][0]["score"], "3 : 1")


if __name__ == "__main__":
    unittest.main()
