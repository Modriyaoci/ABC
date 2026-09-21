import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import server


class DayCompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime(2026, 9, 21, 20, tzinfo=server.BEIJING_TZ)
        self.payload = {
            "meta": {"generatedAt": "v1", "officialDays": {"TTE": ["2026-09-21"]}},
            "records": [{"id": "TTE:tie", "sport": "TTE", "date": "2026-09-21",
                         "time": "19:00", "status": "OFFICIAL", "isLive": False,
                         "category": "男子团体", "score": "3 : 2"}],
        }
        self.data = self.root / "schedule.json"
        self.status = self.root / "status.json"
        self.data.write_text(json.dumps(self.payload))
        self.status.write_text(json.dumps({"lastSuccess": "2026-09-21T08:01:00+08:00"}))
        self.app = server.AppState(self.data, self.status, lambda: self.now)
        self.cache = server.OfficialCache()
        self.enterContext(patch.object(server, "OFFICIAL_CACHE", self.cache))

    def completed(self):
        return self.app.snapshot()["todayCompleted"]

    def test_stop_all_automatic_paths_only_after_all_sports_finish(self):
        self.app.payload["meta"]["officialDays"]["BDM"] = ["2026-09-21"]
        match = {**self.payload["records"][0], "id": "BDM:tie", "sport": "BDM", "status": "RUNNING"}
        self.app.payload["records"].append(match)
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.assertFalse(self.completed())
            self.assertEqual(self.app.tick(), "live")
            start.assert_called_once_with("live")
        match["status"] = "OFFICIAL"
        snapshot = self.app.snapshot()
        self.assertTrue(snapshot["todayCompleted"])
        self.assertEqual(snapshot["completionDate"], "2026-09-21")
        self.assertFalse(snapshot["automaticSyncAllowed"])
        self.assertFalse(snapshot["liveActive"])
        self.assertIsNone(snapshot["nextLiveSync"])
        self.assertEqual(snapshot["nextAutomaticSync"], "2026-09-22T08:00:00+08:00")
        self.assertIsNone(self.app.tick())
        with patch("server.threading.Thread") as thread:
            for reason in ("live", "scheduled", "retry"):
                self.assertFalse(self.app.start_sync(reason))
            thread.assert_not_called()
            self.assertTrue(self.app.start_sync("manual"))
            thread.return_value.start.assert_called_once()

    def test_empty_missing_or_unconfirmed_schedule_does_not_stop(self):
        for status in ("RUNNING", "SCHEDULED", "START_LIST", "POSTPONED", "SUSPENDED", "UNOFFICIAL", "", "NEW_STATUS"):
            with self.subTest(status=status):
                self.app.payload["records"][0]["status"] = status
                self.assertFalse(self.completed())
        self.app.payload["records"][0]["status"] = "OFFICIAL"
        self.app.payload["records"][0]["isLive"] = True
        self.assertFalse(self.completed())
        self.app.payload["records"][0]["isLive"] = False
        self.app.payload["meta"]["officialDays"]["BDM"] = ["2026-09-21"]
        self.assertFalse(self.completed(), "a missing sport's feed must not count as finished")
        self.app.payload["records"] = []
        self.assertFalse(self.completed())

    def test_future_matches_do_not_block_today_and_cancellations_are_terminal(self):
        self.app.payload["records"][0]["status"] = "CANCELLED"
        self.app.payload["records"].append({**self.payload["records"][0], "id": "TTE:tomorrow",
                                          "date": "2026-09-22", "status": "SCHEDULED"})
        self.assertTrue(self.completed())
        self.now = self.now.astimezone(timezone.utc)
        self.assertTrue(self.completed(), "completion date uses Beijing time")

    def test_stale_full_sync_cannot_stop_and_next_morning_resumes(self):
        self.app.status["lastSuccess"] = "2026-09-20T08:01:00+08:00"
        self.assertFalse(self.completed())
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.assertEqual(self.app.tick(), "scheduled")
            start.assert_called_once_with("scheduled")
        self.app.status["lastSuccess"] = "2026-09-21T08:01:00+08:00"
        self.assertTrue(self.completed())
        self.now += timedelta(hours=12)
        self.assertFalse(self.completed())
        with patch.object(self.app, "start_sync", return_value=True) as start:
            self.assertEqual(self.app.tick(), "scheduled")
            start.assert_called_once_with("scheduled")

    def test_restart_retains_completion_and_manual_changes_can_resume_polling(self):
        restarted = server.AppState(self.data, self.status, lambda: self.now)
        self.assertTrue(restarted.snapshot()["todayCompleted"])
        changed = copy.deepcopy(self.payload)
        changed["records"][0]["status"] = "SCHEDULED"
        changed["meta"]["generatedAt"] = "v2"
        restarted.full_sync = Mock(return_value=changed)
        restarted._run_sync("manual")
        self.assertFalse(restarted.snapshot()["todayCompleted"])
        self.assertTrue(restarted.snapshot()["automaticSyncAllowed"])
        self.now += timedelta(seconds=server.LIVE_INTERVAL)
        with patch.object(restarted, "start_sync", return_value=True) as start:
            self.assertEqual(restarted.tick(), "live")
            start.assert_called_once_with("live")

    def test_finalize_only_viewed_today_panels_then_automatic_requests_read_cache(self):
        self.app.payload["records"][0]["status"] = "RUNNING"
        self.cache.values[("match", "TTE:tie")] = (0, {"available": True, "score": "2 : 2"})
        self.cache.values[("match", "TTE:yesterday")] = (0, {"available": True})
        self.cache.values[("tournament", "TTE")] = (0, {"available": True, "events": []})
        self.cache.values[("tournament", "TEN")] = (0, {"available": True, "events": []})
        final = copy.deepcopy(self.payload)
        final["meta"]["generatedAt"] = "v2"
        self.app.live_sync = Mock(return_value=final)
        with patch.object(server, "get_match_details", return_value={"available": True, "score": "3 : 2"}) as match, \
             patch.object(server, "get_tournament", return_value={"available": True, "events": ["final"]}) as tournament:
            self.app._run_sync("live")
            self.assertTrue(self.completed())
            match.assert_called_once()
            tournament.assert_called_once()
            self.enterContext(patch.object(server, "STATE", self.app))
            for path in ("/api/match?id=TTE%3Atie&automatic=1", "/api/tournament?sport=TTE&automatic=1"):
                for _ in range(2):
                    handler = object.__new__(server.RequestHandler)
                    handler.path = path
                    handler._send_json = Mock()
                    handler.do_GET()
                    value = handler._send_json.call_args.args[0]
                    self.assertTrue(value["automaticSyncPaused"])
                    self.assertFalse(value["stale"])
            match.assert_called_once()
            tournament.assert_called_once()
            self.assertEqual(self.cache.peek(("match", "TTE:tie"))["score"], "3 : 2")

    def test_failed_final_detail_remains_marked_stale_without_restart_loop(self):
        self.app.payload["records"][0]["status"] = "RUNNING"
        self.cache.values[("match", "TTE:tie")] = (0, {"available": True, "score": "2 : 2"})
        self.app.live_sync = Mock(return_value=self.payload)
        with patch.object(server, "get_match_details", side_effect=RuntimeError("offline")) as match:
            self.app._run_sync("live")
            self.assertTrue(self.completed())
            self.assertTrue(self.cache.peek(("match", "TTE:tie"))["stale"])
            self.assertIsNone(self.app.tick())
            match.assert_called_once()

    def test_late_detail_result_is_confirmed_before_stopping(self):
        for pending in ({"available": True, "status": "RUNNING"},
                        {"available": True, "status": "START_LIST"},
                        {"available": True, "subMatches": [{"isLive": True}]},
                        {"available": False}):
            with self.subTest(pending=pending):
                self.app.payload = copy.deepcopy(self.payload)
                self.app.payload["records"][0]["status"] = "RUNNING"
                self.cache.values[("match", "TTE:tie")] = (0, {"available": True, "score": "2 : 2"})
                self.app.live_sync = Mock(return_value=self.payload)
                with patch.object(server, "get_match_details", side_effect=[pending, {"available": True, "status": "OFFICIAL", "score": "3 : 2"}]) as match:
                    self.app._run_sync("live")
                    self.assertFalse(self.completed())
                    self.assertNotIn("completedForDate", self.cache.peek(("match", "TTE:tie")))
                    self.now += timedelta(seconds=server.LIVE_INTERVAL)
                    self.app._run_sync("live")
                    self.assertTrue(self.completed())
                    self.assertEqual(match.call_count, 2)
                    self.assertEqual(self.cache.peek(("match", "TTE:tie"))["score"], "3 : 2")

    def test_details_preserve_official_parent_status_for_final_confirmation(self):
        from details_service import get_match_details
        out = get_match_details("TTE:tie", lambda _: {"Info": {"Status": "RUNNING", "IsLive": True}})
        self.assertEqual(out["status"], "RUNNING")
        self.assertTrue(out["isLive"])


if __name__ == "__main__":
    unittest.main()
