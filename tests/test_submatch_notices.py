import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from server import AppState, BEIJING_TZ, team_submatch_order


class TeamSubmatchNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.data_file = root / "schedule.json"
        self.status_file = root / "status.json"
        self.now = datetime(2026, 9, 20, 14, 0, tzinfo=BEIJING_TZ)
        self.record = {
            "id": "BDM:W.TEAM.8FNL.00070000", "sport": "BDM",
            "category": "女子团体", "eventCode": "W.TEAM", "matchup": "哈萨克斯坦 vs 印度",
        }
        self.data_file.write_text(json.dumps({"meta": {}, "records": [self.record]}), encoding="utf-8")
        self.state = self.new_state()

    def new_state(self):
        return AppState(self.data_file, self.status_file, clock=lambda: self.now)

    @staticmethod
    def details(slots=(1, 2, 3, 4, 5)):
        return {
            "subMatches": [
                {"id": f"BDM:tie.0007000{slot}", "number": index,
                 "home": f"Player {slot}", "away": f"Opponent {slot}",
                 "homeScore": "0", "awayScore": "0", "status": "START_LIST"}
                for index, slot in enumerate(slots, 1)
            ],
        }

    def test_first_seen_order_is_persisted_without_a_notice(self):
        self.state.observe_match_details(self.record, self.details((1, 3, 5, 4, 2)))
        self.assertFalse(self.state.snapshot()["scheduleChanged"])
        restarted = self.new_state()
        self.assertEqual(restarted.team_submatch_orders, self.state.team_submatch_orders)
        self.assertNotIn("teamSubmatchOrders", restarted.snapshot())

    def test_score_status_and_response_array_order_do_not_trigger_notice(self):
        self.state.observe_match_details(self.record, self.details())
        changed = self.details()
        child = changed["subMatches"][2]
        child.update(homeScore="1", awayScore="0", status="RUNNING", isLive=True)
        child["sections"] = [{"rows": [["第1局", "21", "12"]]}]
        child["homePlayers"] = [{"name": "New photo metadata", "photo": "photo.jpg"}]
        changed["subMatches"].reverse()
        self.state.observe_match_details(self.record, changed)
        self.assertFalse(self.state.snapshot()["scheduleChanged"])

    def test_actual_reorder_latches_notice_and_survives_restart(self):
        self.state.observe_match_details(self.record, self.details())
        # A restart must preserve the earlier order, not make this first-seen.
        self.state = self.new_state()
        self.now += timedelta(minutes=5)
        self.state.observe_match_details(self.record, self.details((1, 3, 5, 4, 2)))
        status = self.state.snapshot()
        self.assertFalse(status["scheduleChanged"])
        self.assertEqual(status["teamScheduleChanges"], {
            self.record["id"]: {
                "changedAt": status["teamScheduleChanges"][self.record["id"]]["changedAt"],
                "matchup": self.record["matchup"], "fields": ["subMatchOrder"],
            },
        })
        changed_at = status["teamScheduleChanges"][self.record["id"]]["changedAt"]
        self.state = self.new_state()
        self.now += timedelta(minutes=5)
        self.state.observe_match_details(self.record, self.details((1, 3, 5, 4, 2)))
        self.assertFalse(self.state.snapshot()["scheduleChanged"])
        self.assertIn(self.record["id"], self.state.snapshot()["teamScheduleChanges"])
        self.assertEqual(self.state.snapshot()["teamScheduleChanges"][self.record["id"]]["changedAt"], changed_at)

    def test_partial_stale_or_ambiguous_data_does_not_replace_known_order(self):
        self.state.observe_match_details(self.record, self.details())
        original = copy.deepcopy(self.state.team_submatch_orders)
        partial = self.details((1, 3))
        stale = {**self.details((1, 3, 5, 4, 2)), "stale": True}
        duplicate_number = self.details()
        duplicate_number["subMatches"][1]["number"] = 1
        duplicate_id = self.details()
        duplicate_id["subMatches"][1]["id"] = duplicate_id["subMatches"][0]["id"]
        unknown_id = self.details()
        unknown_id["subMatches"][1]["id"] = "BDM:sub-2"
        replacement = self.details((1, 3, 5, 4, 8))
        for details in ({}, {"subMatches": []}, partial, stale, duplicate_number, duplicate_id, unknown_id, replacement):
            with self.subTest(details=details):
                self.state.observe_match_details(self.record, details)
                self.assertEqual(self.state.team_submatch_orders, original)
                self.assertFalse(self.state.snapshot()["scheduleChanged"])
        self.state.observe_match_details(self.record, self.details((1, 3, 5, 4, 2)))
        self.assertFalse(self.state.snapshot()["scheduleChanged"])
        self.assertIn(self.record["id"], self.state.snapshot()["teamScheduleChanges"])

    def test_more_complete_initial_data_establishes_baseline_without_notice(self):
        self.state.observe_match_details(self.record, self.details((1, 3)))
        self.state.observe_match_details(self.record, self.details((1, 3, 5, 4, 2)))
        self.assertFalse(self.state.snapshot()["scheduleChanged"])
        self.assertEqual(len(self.state.team_submatch_orders[self.record["id"]]), 5)

    def test_full_schedule_sync_does_not_overwrite_team_schedule_changes(self):
        self.state.observe_match_details(self.record, self.details())
        self.state.observe_match_details(self.record, self.details((1, 3, 5, 4, 2)))
        before = copy.deepcopy(self.state.snapshot()["teamScheduleChanges"])
        self.state.full_sync = lambda *_args: {
            "meta": {"generatedAt": "v2"}, "records": [self.record],
        }
        self.state._run_sync("scheduled")
        self.assertEqual(self.state.snapshot()["teamScheduleChanges"], before)

    def test_only_supported_team_ties_are_observed(self):
        for record in (
            {**self.record, "sport": "TEN"},
            {**self.record, "category": "女子单打", "eventCode": "W.SINGLES"},
        ):
            self.assertIsNone(team_submatch_order(record, self.details()))


if __name__ == "__main__":
    unittest.main()
