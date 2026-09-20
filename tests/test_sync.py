import json
import unittest
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

from server import next_eight
from sync_service import _decode_response, _score, normalize_unit, preserve_known_matchups


class SyncServiceTests(unittest.TestCase):
    def test_numeric_zero_score_is_preserved(self):
        self.assertEqual(_score({"Home": {"Result": 3}, "Away": {"Result": 0}}), "3 : 0")

    def test_pending_refresh_keeps_last_published_matchup(self):
        previous = [{
            "id": "TTE:match-1", "home": {"Org": "CHN"}, "away": {"Org": "JPN"},
            "matchup": "中国 vs 日本", "score": "3 : 1",
        }]
        incoming = [{
            "id": "TTE:match-1", "home": {}, "away": {},
            "matchup": "对阵待定", "score": "待赛",
        }, {
            "id": "TTE:match-2", "home": {}, "away": {},
            "matchup": "对阵待定", "score": "待赛",
        }]
        result = preserve_known_matchups(previous, incoming)
        self.assertEqual(result[0]["matchup"], "中国 vs 日本")
        self.assertEqual(result[0]["score"], "待赛")
        self.assertEqual(result[1]["matchup"], "对阵待定")

        partial = [{"id": "TTE:match-1", "home": {"Org": "CHN"}, "away": {}, "matchup": "中国 vs 待定"}]
        self.assertEqual(preserve_known_matchups(previous, partial)[0]["matchup"], "中国 vs 日本")

    def test_cricket_schedule_score_shows_runs_only(self):
        self.assertEqual(_score({"Home": {"Result": "92 - 7"}, "Away": {"Result": "91 - 4"}}, "CKT"), "92 : 91")

    def test_decodes_official_compressed_payload(self):
        expected = [{"raw": "2026-09-19"}]
        compressed = zlib.compress(json.dumps(expected).encode("utf-8"))
        wire_body = compressed.decode("latin-1").encode("utf-8")
        self.assertEqual(_decode_response(wire_body), expected)

    def test_normalizes_japan_time_to_beijing_time(self):
        unit = {
            "Key": "test",
            "DateTimeRaw": "2026-09-19T10:00:00+09:00",
            "Type": "T",
            "EventDesc": "Women",
            "UnitDesc": "Round Match 2",
            "VenueDesc": "Kasugai City Gymnasium",
            "Status": "SCHEDULED",
            "Home": {"Org": "KAZ", "Name": "Kazakhstan", "Result": ""},
            "Away": {"Org": "CHN", "Name": "People's Republic of China", "Result": ""},
        }
        result = normalize_unit(unit, "HBL")
        self.assertEqual(result["date"], "2026-09-19")
        self.assertEqual(result["time"], "09:00")
        self.assertEqual(result["matchup"], "哈萨克斯坦 vs 中国")
        self.assertEqual(result["stage"], "循环赛第2场")
        self.assertEqual(result["score"], "待赛")

    def test_next_daily_sync_uses_beijing_eight(self):
        timezone = ZoneInfo("Asia/Shanghai")
        before = datetime(2026, 9, 17, 7, 59, tzinfo=timezone)
        after = datetime(2026, 9, 17, 8, 1, tzinfo=timezone)
        self.assertEqual(next_eight(before), datetime(2026, 9, 17, 8, 0, tzinfo=timezone))
        self.assertEqual(next_eight(after), datetime(2026, 9, 18, 8, 0, tzinfo=timezone))


if __name__ == "__main__":
    unittest.main()
