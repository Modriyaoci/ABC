import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import server
from sync_service import SyncError


class AutomaticWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / "schedule.json"
        self.status_file = Path(self.directory.name) / "status.json"
        self.now = datetime(2026, 9, 21, 8, tzinfo=server.BEIJING_TZ)
        self.payload = {
            "meta": {"generatedAt": "v1", "officialDays": {"TTE": ["2026-09-21"]}},
            "records": [{"id": "TTE:tie", "sport": "TTE", "category": "女子团体", "isLive": True}],
        }
        self.data.write_text(json.dumps(self.payload))
        self.app = server.AppState(self.data, self.status_file, lambda: self.now)

    def test_window_boundaries_are_beijing_time(self):
        for hour, minute, allowed in [(7, 59, False), (8, 0, True), (22, 59, True), (23, 0, False), (0, 0, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.now = self.now.replace(hour=hour, minute=minute)
                self.assertEqual(server.automatic_sync_allowed(self.now), allowed)
                self.assertEqual(server.automatic_sync_allowed(self.now.astimezone(timezone.utc)), allowed)
                snapshot = self.app.snapshot()
                self.assertEqual(snapshot["automaticSyncAllowed"], allowed)
                self.assertEqual(snapshot["automaticWindow"], {"start": "08:00", "end": "23:00", "timezone": "Asia/Shanghai"})
                self.assertEqual(snapshot["liveActive"], allowed)
                if not allowed:
                    self.assertIsNone(snapshot["nextLiveSync"])
                self.assertTrue(server.automatic_sync_allowed(datetime.fromisoformat(snapshot["nextAutomaticSync"])))

    def test_stale_or_missing_schedule_waits_for_eight(self):
        self.data.unlink()
        self.now = self.now.replace(hour=0)
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.assertIsNone(self.app.tick())
            self.now = self.now.replace(hour=7, minute=59, second=59)
            self.assertIsNone(self.app.tick())
            start.assert_not_called()
            self.assertEqual(self.app.snapshot()["nextAutomaticSync"], "2026-09-21T08:00:00+08:00")
            self.now += timedelta(seconds=1)
            self.assertEqual(self.app.tick(), "scheduled")
            start.assert_called_once_with("scheduled")

    def test_all_automatic_launches_are_rechecked_at_window_boundary(self):
        for hour in (0, 7, 23):
            self.now = self.now.replace(hour=hour)
            for reason in ("scheduled", "retry", "live"):
                with self.subTest(hour=hour, reason=reason), patch("server.threading.Thread") as thread:
                    self.assertFalse(self.app.start_sync(reason))
                    thread.assert_not_called()
                    self.assertFalse(self.app.running)
        for hour in (8, 22):
            self.now = self.now.replace(hour=hour)
            with patch("server.threading.Thread") as thread:
                self.assertTrue(self.app.start_sync("scheduled"))
                thread.return_value.start.assert_called_once()
            self.app.running = False

    def test_manual_sync_remains_available_at_night(self):
        for hour in (0, 7, 23):
            self.now = self.now.replace(hour=hour)
            with self.subTest(hour=hour), patch("server.threading.Thread") as thread:
                self.assertTrue(self.app.start_sync("manual"))
                thread.return_value.start.assert_called_once()
                self.assertEqual(self.app.status["lastReason"], "manual")
            self.app.running = False

    def test_overdue_full_retry_waits_until_morning_without_rewriting_backoff(self):
        retry_at = "2026-09-21T22:58:00+08:00"
        self.app.status["retryAt"] = retry_at
        self.now = self.now.replace(hour=23)
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.assertIsNone(self.app.tick())
            self.assertEqual(self.app.snapshot()["nextAutomaticSync"], "2026-09-22T08:00:00+08:00")
            self.assertEqual(self.app.status["retryAt"], retry_at)
            start.assert_not_called()
            self.now += timedelta(hours=9)
            self.assertEqual(self.app.tick(), "retry")
            start.assert_called_once_with("retry")

    def test_live_retry_and_next_interval_cannot_launch_at_night(self):
        self.now = self.now.replace(hour=22, minute=59, second=59)
        self.app.status["lastSuccess"] = self.now.replace(hour=8).isoformat()
        self.app.next_live = self.now + timedelta(seconds=server.LIVE_INTERVAL)
        self.app.status["liveRetryAt"] = "2026-09-21T23:00:05+08:00"
        self.assertEqual(self.app.snapshot()["nextLiveSync"], "2026-09-22T08:00:00+08:00")
        self.now += timedelta(seconds=11)
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.assertIsNone(self.app.tick())
            self.assertIsNone(self.app.snapshot()["nextLiveSync"])
            start.assert_not_called()

    def test_late_failure_keeps_retry_delay_but_moves_automatic_run_to_morning(self):
        self.now = self.now.replace(hour=22, minute=59)
        self.app.full_sync = Mock(side_effect=SyncError("offline"))
        self.app._run_sync("scheduled")
        self.assertEqual(self.app.status["retryAt"], "2026-09-21T23:04:00+08:00")
        self.assertEqual(self.app.snapshot()["nextAutomaticSync"], "2026-09-22T08:00:00+08:00")

    def test_real_rate_limit_deadline_is_preserved_past_morning(self):
        self.now = self.now.replace(hour=7, minute=30)
        self.app.full_sync = Mock(side_effect=SyncError("HTTP 429"))
        self.app._run_sync("manual")
        self.assertEqual(self.app.status["retryAt"], "2026-09-21T08:30:00+08:00")
        self.now = self.now.replace(hour=8, minute=0)
        self.assertEqual(self.app.snapshot()["nextAutomaticSync"], "2026-09-21T08:30:00+08:00")
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.app.payload = {"meta": {}, "records": []}
            self.assertIsNone(self.app.tick())
            start.assert_not_called()

    def test_missed_days_catch_up_once_in_allowed_window(self):
        self.app.status["lastSuccess"] = "2026-09-17T08:01:00+08:00"
        self.now = self.now.replace(hour=7)
        full_sync = Mock(return_value=self.payload)
        self.app.full_sync = full_sync

        def run_now(reason):
            self.app._run_sync(reason)
            return True

        with patch.object(self.app, "start_sync", side_effect=run_now):
            self.assertIsNone(self.app.tick())
            self.now = self.now.replace(hour=8)
            self.assertEqual(self.app.tick(), "scheduled")
            self.assertIsNone(self.app.tick())
        full_sync.assert_called_once()


class AutomaticDetailsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 9, 21, 23, tzinfo=server.BEIJING_TZ)
        root = Path(self.directory.name)
        self.app = server.AppState(root / "schedule.json", root / "status.json", lambda: self.now)
        self.app.payload = {"records": [{"id": "TTE:tie", "sport": "TTE", "category": "女子团体"}]}
        self.cache = server.OfficialCache()
        self.patches = self.enterContext(ExitStack())
        self.patches.enter_context(patch.object(server, "STATE", self.app))
        self.patches.enter_context(patch.object(server, "OFFICIAL_CACHE", self.cache))
        if hasattr(server, "DETAILS_CACHE"):
            self.patches.enter_context(patch.object(server, "DETAILS_CACHE", server.PersistentDetailsCache(root / "details.json")))
        self.match_loader = self.patches.enter_context(patch.object(server, "get_match_details", return_value={"available": True, "score": "8 : 7"}))
        self.tournament_loader = self.patches.enter_context(patch.object(server, "get_tournament", return_value={"available": True, "groups": []}))

    def request(self, path):
        handler = object.__new__(server.RequestHandler)
        handler.path = path
        handler._send_json = Mock()
        handler.do_GET()
        return handler._send_json.call_args.args[0]

    def test_night_automatic_requests_do_not_load_even_when_cache_is_empty(self):
        for path in ("/api/match?id=TTE%3Atie&automatic=1", "/api/tournament?sport=TTE&automatic=1"):
            with self.subTest(path=path):
                result = self.request(path)
                self.assertTrue(result["automaticSyncPaused"])
                self.assertFalse(result["available"])
                self.assertTrue(result["unavailable"])
        self.match_loader.assert_not_called()
        self.tournament_loader.assert_not_called()

    def test_expired_cached_detail_is_read_without_mutation_or_loader(self):
        details = {"available": True, "score": "11 : 9"}
        self.cache.values[("match", "TTE:tie")] = (0, details)
        result = self.request("/api/match?id=TTE%3Atie&automatic=1")
        self.assertEqual(result["score"], "11 : 9")
        self.assertTrue(result["stale"])
        self.assertNotIn("stale", details)
        self.match_loader.assert_not_called()

    def test_manual_details_and_tournament_still_load_overnight(self):
        result = self.request("/api/match?id=TTE%3Atie")
        self.assertEqual(result["score"], "8 : 7")
        self.assertNotIn("automaticSyncPaused", result)
        self.request("/api/tournament?sport=TTE")
        self.match_loader.assert_called_once()
        self.tournament_loader.assert_called_once()
        self.now += timedelta(hours=1)
        cached = self.request("/api/tournament?sport=TTE&automatic=1")
        self.assertEqual(cached["groups"], [])
        self.tournament_loader.assert_called_once()

    def test_automatic_details_resume_at_eight(self):
        self.now += timedelta(hours=9)
        result = self.request("/api/match?id=TTE%3Atie&automatic=1")
        self.assertNotIn("automaticSyncPaused", result)
        self.match_loader.assert_called_once()
        self.request("/api/tournament?sport=TTE&automatic=1")
        self.tournament_loader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
