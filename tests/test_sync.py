import json
import io
import urllib.error
import unittest
import zlib
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from server import next_eight
from sync_service import (
    _decode_response,
    _score,
    fetch_official_json,
    normalize_unit,
    preserve_known_matchups,
    preserve_missing_schedule_rows,
)


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

    def test_truncated_daily_feed_keeps_omitted_future_rows(self):
        previous = [
            {"id": "TEN:a", "sport": "TEN", "date": "2026-09-27"},
            {"id": "TEN:b", "sport": "TEN", "date": "2026-09-27"},
            {"id": "TEN:c", "sport": "TEN", "date": "2026-09-27"},
        ]
        incoming = [previous[0]]
        result = preserve_missing_schedule_rows(previous, incoming)
        self.assertEqual({row["id"] for row in result}, {"TEN:a", "TEN:b", "TEN:c"})

    def test_same_count_removals_are_authoritative(self):
        previous = [
            {"id": "TEN:a", "sport": "TEN", "date": "2026-09-27"},
            {"id": "TEN:b", "sport": "TEN", "date": "2026-09-27"},
        ]
        incoming = [
            {"id": "TEN:a", "sport": "TEN", "date": "2026-09-27"},
            {"id": "TEN:c", "sport": "TEN", "date": "2026-09-27"},
        ]
        result = preserve_missing_schedule_rows(previous, incoming)
        self.assertEqual({row["id"] for row in result}, {"TEN:a", "TEN:c"})

    def test_cricket_schedule_score_shows_runs_only(self):
        self.assertEqual(_score({"Home": {"Result": "92 - 7"}, "Away": {"Result": "91 - 4"}}, "CKT"), "92 : 91")

    def test_decodes_official_compressed_payload(self):
        expected = [{"raw": "2026-09-19"}]
        compressed = zlib.compress(json.dumps(expected).encode("utf-8"))
        wire_body = compressed.decode("latin-1").encode("utf-8")
        self.assertEqual(_decode_response(wire_body), expected)

    def test_official_fetch_retries_rate_limit_without_cache_buster(self):
        body = json.dumps([{"raw": "2026-09-19"}]).encode("utf-8")

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return body

        limited = urllib.error.HTTPError(
            "https://back.results.asiangames2026.org/test",
            429,
            "Too Many Requests",
            {"Retry-After": "3"},
            io.BytesIO(),
        )
        with patch("sync_service._wait_for_request"), \
                patch("sync_service._set_rate_limit_cooldown") as cooldown, \
                patch("sync_service.time.sleep"), \
                patch("sync_service._open_official", side_effect=[limited, Response()]) as open_url:
            self.assertEqual(fetch_official_json("/test", retries=2), [{"raw": "2026-09-19"}])

        self.assertEqual(cooldown.call_args.args[0], 8)
        first_request = open_url.call_args_list[0].args[0]
        self.assertNotIn("_=", first_request.full_url)

    def test_official_quota_429_stops_without_retry_after(self):
        limited = urllib.error.HTTPError(
            "https://back.results.asiangames2026.org/test",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(),
        )
        with patch("sync_service._wait_for_request"), \
                patch("sync_service._set_rate_limit_cooldown"), \
                patch("sync_service.time.sleep"), \
                patch("sync_service._open_official", side_effect=limited) as open_url:
            with self.assertRaisesRegex(Exception, "HTTP 429"):
                fetch_official_json("/test", retries=3)
        self.assertEqual(open_url.call_count, 1)

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
