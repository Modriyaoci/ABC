import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from server import (
    AppState,
    BEIJING_TZ,
    LIVE_INTERVAL,
    RATE_LIMIT_RETRY_INTERVAL,
    match_detail_ttl,
    next_eight,
    schedule_changes,
)
from sync_service import SyncError
from live_service import live_targets, sync_live


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / "schedule.json"
        self.status = Path(self.directory.name) / "status.json"
        self.now = datetime(2026, 9, 17, 7, 59, 59, tzinfo=BEIJING_TZ)
        self.payload = {"meta": {"generatedAt": "v1", "officialDays": {"TEN": ["2026-09-27"]}}, "records": []}
        self.data.write_text(json.dumps(self.payload))
        self.status.write_text(json.dumps({"lastSuccess": "2026-09-16T08:01:00+08:00"}))
        self.app = AppState(self.data, self.status, lambda: self.now)

    def run_due_synchronously(self, reason):
        self.app.running = True
        self.app._run_sync(reason)
        return True

    def test_daily_deadline_and_wake_catchup_once(self):
        self.app.full_sync = Mock(return_value=self.payload)
        with patch.object(self.app, "start_sync", side_effect=self.run_due_synchronously):
            self.assertIsNone(self.app.tick())
            self.now += timedelta(seconds=1)
            self.assertEqual(self.app.tick(), "scheduled")
            self.assertIsNone(self.app.tick())
            self.now += timedelta(days=4, hours=3)
            self.assertEqual(self.app.tick(), "scheduled")
            self.assertIsNone(self.app.tick())
        self.assertEqual(self.app.full_sync.call_count, 2)
        restarted = AppState(self.data, self.status, lambda: self.now)
        self.assertEqual(restarted._full_due(self.now), next_eight(self.now))

    def test_failed_daily_sync_retries_and_preserves_data(self):
        previous = self.data.read_bytes()
        self.now += timedelta(seconds=1)
        self.app.full_sync = Mock(side_effect=[SyncError("offline"), self.payload])
        with patch.object(self.app, "start_sync", side_effect=self.run_due_synchronously):
            self.assertEqual(self.app.tick(), "scheduled")
            self.assertEqual(self.data.read_bytes(), previous)
            self.assertEqual(self.app.snapshot()["nextAutomaticSync"], "2026-09-17T08:05:00+08:00")
            self.now += timedelta(seconds=299)
            self.assertIsNone(self.app.tick())
            self.now += timedelta(seconds=1)
            self.assertEqual(self.app.tick(), "retry")
            self.assertIsNone(self.app.status["retryAt"])
            self.assertIsNone(self.app.status["lastError"])

    def test_live_interval_no_overlap_and_retry_backoff(self):
        self.now = self.now.replace(hour=12)
        self.app.status["lastSuccess"] = self.now.isoformat()
        self.app.payload["meta"]["officialDays"]["TEN"] = ["2026-09-17"]
        self.app.live_sync = Mock(side_effect=[SyncError("offline"), SyncError("offline"), self.payload])
        with patch.object(self.app, "start_sync", side_effect=self.run_due_synchronously):
            self.app.running = True
            self.assertIsNone(self.app.tick())
            self.app.running = False
            self.assertEqual(self.app.tick(), "live")
            self.now += timedelta(seconds=LIVE_INTERVAL - 1)
            self.assertIsNone(self.app.tick())
            self.now += timedelta(seconds=1)
            self.assertEqual(self.app.tick(), "live")
            self.now += timedelta(seconds=LIVE_INTERVAL * 2 - 1)
            self.assertIsNone(self.app.tick())
            self.now += timedelta(seconds=1)
            self.assertEqual(self.app.tick(), "live")
            self.assertIsNone(self.app.status["liveRetryAt"])
            self.assertIsNone(self.app.status["lastLiveError"])
        self.assertEqual(self.app.live_sync.call_count, 3)

    def test_timezone_conversion_for_next_eight(self):
        utc = datetime(2026, 9, 17, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(next_eight(utc).isoformat(), "2026-09-18T08:00:00+08:00")

    def test_live_failure_does_not_mark_daily_job_successful(self):
        self.app.status["lastSuccess"] = "2026-09-17T08:01:00+08:00"
        self.app.status["retryAt"] = "2026-09-17T12:05:00+08:00"
        self.app.live_sync = Mock(return_value=self.payload)
        self.app._run_sync("live")
        self.assertEqual(self.app.status["retryAt"], "2026-09-17T12:05:00+08:00")
        self.assertEqual(self.app.status["lastSuccess"], "2026-09-17T08:01:00+08:00")

    def test_live_interval_and_team_detail_cache_window(self):
        self.assertEqual(LIVE_INTERVAL, 10)
        self.assertEqual(self.app.snapshot()["liveIntervalSeconds"], 10)
        self.assertEqual(match_detail_ttl({"isLive": True}), 4)
        self.assertEqual(match_detail_ttl({"isLive": False, "sport": "BDM", "category": "女子团体"}), 4)
        self.assertEqual(match_detail_ttl({"isLive": False, "sport": "TTE", "eventCode": "M.TEAM----------------"}), 4)
        self.assertEqual(match_detail_ttl({"isLive": False, "sport": "VVO", "eventCode": "M.TEAM----------------"}), 120)
        self.assertEqual(match_detail_ttl({"isLive": False, "status": "RUNNING"}), 4)
        self.assertEqual(match_detail_ttl({"isLive": False, "category": "男子单打"}), 120)

    def test_live_retry_backoff_caps_at_five_minutes(self):
        self.now = self.now.replace(hour=12)
        self.app.status["lastSuccess"] = self.now.isoformat()
        self.app.payload["meta"]["officialDays"]["TEN"] = ["2026-09-17"]
        self.app.status["liveFailures"] = 7
        self.app.live_sync = Mock(side_effect=SyncError("offline"))
        self.app._run_sync("live")
        self.assertEqual(self.app.status["liveRetryAt"], (self.now + timedelta(seconds=300)).isoformat())

    def test_rate_limit_uses_long_backoff_for_live_and_full_sync(self):
        error = SyncError("官网匿名请求额度已用尽（HTTP 429），请稍后重试")
        self.app.status["lastSuccess"] = self.now.isoformat()
        self.app.payload["meta"]["officialDays"]["TEN"] = ["2026-09-17"]
        self.app.live_sync = Mock(side_effect=error)
        self.app._run_sync("live")
        self.assertEqual(
            self.app.status["liveRetryAt"],
            (self.now + timedelta(seconds=RATE_LIMIT_RETRY_INTERVAL)).isoformat(),
        )

        self.app.full_sync = Mock(side_effect=error)
        self.app._run_sync("manual")
        self.assertEqual(
            self.app.status["retryAt"],
            (self.now + timedelta(seconds=RATE_LIMIT_RETRY_INTERVAL)).isoformat(),
        )

    def test_schedule_changes_ignore_live_scores_and_report_time_edits(self):
        old = {"records": [{
            "id": "BDM:tie-7", "date": "2026-09-20", "time": "14:00",
            "category": "女子团体", "stage": "16强赛", "matchup": "哈萨克斯坦 vs 印度",
            "venue": "一宫市综合体育馆", "score": "0 : 0", "status": "RUNNING",
        }]}
        live_only = {"records": [{**old["records"][0], "score": "1 : 0", "status": "OFFICIAL"}]}
        self.assertEqual(schedule_changes(old, live_only), [])
        moved = {"records": [{**old["records"][0], "time": "15:00"}]}
        changes = schedule_changes(old, moved)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["fields"], ["time"])

    def test_schedule_change_notice_survives_later_live_refreshes_and_restart(self):
        old = {
            "meta": {"generatedAt": "v1", "officialDays": {"BDM": ["2026-09-20"]}},
            "records": [{
                "id": "BDM:tie-7", "date": "2026-09-20", "time": "14:00",
                "category": "女子团体", "stage": "16强赛", "matchup": "哈萨克斯坦 vs 印度",
                "venue": "一宫市综合体育馆", "score": "0 : 0",
            }],
        }
        moved = {
            "meta": {"generatedAt": "v2", "officialDays": {"BDM": ["2026-09-20"]}},
            "records": [{**old["records"][0], "time": "15:00", "score": "1 : 0"}],
        }
        score_update = {
            "meta": {**moved["meta"], "generatedAt": "v3"},
            "records": [{**moved["records"][0], "score": "2 : 0"}],
        }
        self.app.payload = old
        self.app.live_sync = Mock(side_effect=[moved, score_update])

        # The first live update detects the schedule edit and persists the
        # notice.  A later score-only refresh must not clear it.
        self.app._run_sync("live")
        self.assertTrue(self.app.status["scheduleChanged"])
        self.assertEqual(self.app.status["scheduleChangeCount"], 1)
        changed_at = self.app.status["scheduleChangeAt"]
        self.now += timedelta(seconds=LIVE_INTERVAL)
        self.app._run_sync("live")
        self.assertTrue(self.app.status["scheduleChanged"])
        self.assertEqual(self.app.status["scheduleChangeCount"], 1)
        self.assertEqual(self.app.status["scheduleChangeAt"], changed_at)

        saved = json.loads(self.status.read_text())
        self.assertTrue(saved["scheduleChanged"])
        self.assertEqual(saved["scheduleChangeCount"], 1)
        restarted = AppState(self.data, self.status, lambda: self.now)
        self.assertTrue(restarted.status["scheduleChanged"])
        self.assertEqual(restarted.status["scheduleChangeCount"], 1)


class LiveMergeTests(unittest.TestCase):
    def test_official_japan_date_and_cross_midnight_live_match(self):
        payload = {"meta": {"officialDays": {"TEN": ["2026-09-18"]}}, "records": [
            {"sport": "VVO", "sourceDate": "2026-09-17", "isLive": True},
            {"sport": "HBL", "sourceDate": "2026-09-17", "isLive": False},
        ]}
        now = datetime(2026, 9, 17, 23, 30, tzinfo=BEIJING_TZ)
        self.assertEqual(live_targets(payload, now), [("TEN", "2026-09-18"), ("VVO", "2026-09-17")])

    def test_only_today_is_replaced_and_error_keeps_entire_snapshot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            future = {"id": "TEN:future", "sport": "TEN", "date": "2026-09-27", "time": "09:00", "sourceDate": "2026-09-27"}
            current = {"id": "VVO:old", "sport": "VVO", "date": "2026-09-17", "time": "09:00", "sourceDate": "2026-09-17"}
            old = {"meta": {"generatedAt": "old", "officialDays": {"VVO": ["2026-09-17"]}}, "records": [future, current]}
            path.write_text(json.dumps(old))
            now = datetime(2026, 9, 17, 12, tzinfo=BEIJING_TZ)
            new_unit = {"Key": "new", "DateTimeRaw": "2026-09-17T11:00:00+09:00", "Status": "OFFICIAL", "Home": {"Result": "3"}, "Away": {"Result": "0"}}
            with patch("live_service.fetch_official_json", return_value=[new_unit]):
                result = sync_live(path, now)
            self.assertEqual([r["id"] for r in result["records"]], ["VVO:new", "TEN:future"])
            self.assertEqual(result["records"][1], future)
            successful = path.read_bytes()
            with patch("live_service.fetch_official_json", return_value=[]):
                result = sync_live(path, now)
            self.assertEqual(path.read_bytes(), successful)

    def test_live_now_aggregate_merges_one_current_score_request(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            old = {
                "meta": {"generatedAt": "old", "officialDays": {"VVO": ["2026-09-17"]}},
                "records": [{
                    "id": "VVO:live", "sport": "VVO", "sourceDate": "2026-09-17",
                    "date": "2026-09-17", "time": "10:00", "home": {"Org": "CHN"},
                    "away": {"Org": "JPN"}, "matchup": "中国 vs 日本", "score": "0 : 0",
                }],
            }
            path.write_text(json.dumps(old))
            now = datetime(2026, 9, 17, 12, tzinfo=BEIJING_TZ)
            live_unit = {
                "Disc": "VVO", "Key": "live", "DateTimeRaw": "2026-09-17T10:00:00+09:00",
                "Type": "T", "EventDesc": "Men's Team", "PhaseDesc": "Preliminary Round",
                "UnitDesc": "Match 1", "VenueDesc": "Park Arena Komaki", "Status": "RUNNING",
                "Home": {"Org": "CHN", "Result": "12"},
                "Away": {"Org": "JPN", "Result": "10"},
            }
            with patch("live_service.fetch_official_json", return_value=[live_unit]) as fetch:
                result = sync_live(path, now)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], "/s/AG2026/en/ALL/schedule/live-now")
            self.assertEqual(result["records"][0]["id"], "VVO:live")
            self.assertEqual(result["records"][0]["score"], "12 : 10")

    def test_live_refresh_keeps_matchup_when_official_temporarily_omits_teams(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            old = {
                "meta": {"generatedAt": "old", "officialDays": {"TTE": ["2026-09-20"]}},
                "records": [{
                    "id": "TTE:known", "sport": "TTE", "sourceDate": "2026-09-20",
                    "date": "2026-09-20", "time": "10:00", "home": {"Org": "CHN"},
                    "away": {"Org": "JPN"}, "matchup": "中国 vs 日本", "score": "3 : 1",
                }],
            }
            path.write_text(json.dumps(old))
            now = datetime(2026, 9, 20, 12, tzinfo=BEIJING_TZ)
            transient = {
                "Key": "known", "DateTimeRaw": "2026-09-20T10:00:00+09:00",
                "Type": "T", "Status": "START_LIST", "Home": {}, "Away": {},
            }
            with patch("live_service.fetch_official_json", return_value=[transient]):
                result = sync_live(path, now)
            self.assertEqual(result["records"][0]["matchup"], "中国 vs 日本")
            self.assertEqual(result["records"][0]["score"], "待赛")
            successful = path.read_bytes()
            # An empty aggregate live-now response is a successful no-op.
            with patch("live_service.fetch_official_json", return_value=[]):
                result = sync_live(path, now)
            self.assertEqual(result["records"][0]["matchup"], "中国 vs 日本")
            self.assertEqual(path.read_bytes(), successful)


if __name__ == "__main__":
    unittest.main()
