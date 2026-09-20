import unittest

from details_service import get_match_details, get_tournament
from sync_service import SyncError


class DetailsServiceTests(unittest.TestCase):
    def test_periods_are_exposed_as_small_scores(self):
        calls = []

        def fetch(path):
            calls.append(path)
            return {
                "Results": {"Periods": [
                    {"Order": 1, "Desc": "Set 1", "ResHome": "25", "ResAway": "18"},
                    {"Order": 2, "Desc": "Set 2", "ResHome": "25", "ResAway": "22"},
                ]},
                "Competitors": [{"Org": "CHN", "Name": "China"}, {"Org": "PHI", "Name": "Philippines"}],
            }

        out = get_match_details({"id": "VVO:W.TEAM6-------------.GPB-.000100--", "sport": "VVO"}, fetch)
        self.assertTrue(out["available"])
        self.assertEqual((out["home"], out["away"]), ("中国", "菲律宾"))
        self.assertEqual(out["sections"][0]["rows"][0], ["第1局", "25", "18"])
        self.assertIn("/VVO/results/", calls[0])

    def test_cricket_details_expose_runs_wickets_overs(self):
        out = get_match_details("CKT:W.TEAM--------------.QFNL.000200--", lambda _: {
            "Info": {"Status": "OFFICIAL", "Type": "T"},
            "Competitors": [
                {"Org": "PAK", "Name": "Pakistan", "Result": "92 - 7", "Stats": {"ST_TEAM_RUNS": "92", "ST_TEAM_WICKETS": "7", "ST_TEAM_OVERS": "10.4"}},
                {"Org": "THA", "Name": "Thailand", "Result": "91 - 4", "Stats": {"ST_TEAM_RUNS": "91", "ST_TEAM_WICKETS": "4", "ST_TEAM_OVERS": "11.0"}},
            ],
            "Results": {"Periods": []},
        })
        section = next(section for section in out["sections"] if section["title"] == "板球详情")
        self.assertEqual(section["columns"], ["项目", "巴基斯坦", "泰国"])
        self.assertEqual(section["rows"], [["Runs", "92", "91"], ["Wickets", "7", "4"], ["Overs", "10.4", "11.0"]])

    def test_tournament_group_and_bracket(self):
        def fetch(path):
            if path.endswith("/events/phases"):
                return [{"EvKey": "W.TEAM6-------------", "Desc": "Women's Preliminary Round"}]
            if "/groups-v2/" in path:
                return {"Groups": [{"Desc": "Women's Preliminary Round - Pool A", "Competitors": [
                    {"Org": "CHN", "Pos": "1", "Points": "3", "Played": "1", "Won": "1", "Lost": "0"}
                ]}]}
            if "/brackets/" in path:
                return [{"Phases": [{"Code": "QF", "Desc": "Quarterfinals", "Matches": [{
                    "Home": {"Org": "CHN", "Win": True, "Res": "3"},
                    "Away": {"Org": "JPN", "Win": False, "Res": "0"},
                    "Info": {"Key": "qf-1"},
                }]}]}]
            raise AssertionError(path)

        out = get_tournament("VVO", fetcher=fetch)
        self.assertEqual(len(out["events"]), 1)
        event = out["events"][0]
        self.assertEqual(event["groups"][0]["rows"][0][1], "中国")
        self.assertEqual(event["rounds"][0]["matches"][0]["winner"], "home")
        self.assertEqual(event["rounds"][0]["matches"][0]["homeScore"], "3")

    def test_bracket_uses_published_order_and_does_not_mark_canceled_winner(self):
        key = "W.TEAM--------------"
        def fetch(path):
            if path.endswith("events/phases"):
                return [{"EvKey": key, "Desc": "Women"}]
            if "/groups-v2/" in path:
                return {"Groups": []}
            return [{"Code": "FNL", "Phases": [{"Code": key + ".QFNL", "Desc": "Quarterfinals", "Matches": [
                {"Info": {"Key": key + ".QFNL.000200--", "Status": "OFFICIAL"}, "Home": {"Org": "PAK", "Win": True}, "Away": {"Org": "THA"}},
                {"Info": {"Key": key + ".QFNL.000100--", "Status": "CANCELED"}, "Home": {"Org": "BAN", "Win": True}, "Away": {"Org": "CHN"}},
            ]}]}]

        records = [
            {"id": "CKT:" + key + ".QFNL.000100--", "matchup": "孟加拉国 vs 中国", "status": "CANCELED"},
            {"id": "CKT:" + key + ".QFNL.000200--", "matchup": "巴基斯坦 vs 泰国", "status": "OFFICIAL"},
        ]
        rounds = get_tournament("CKT", records=records, fetcher=fetch)["events"][0]["rounds"]
        matches = rounds[0]["matches"]
        self.assertEqual([match["id"].split(".")[-1].rstrip("-") for match in matches], ["000100", "000200"])
        self.assertEqual((matches[0]["home"], matches[0]["away"], matches[0]["winner"], matches[0]["status"]), ("孟加拉国", "中国", "", "CANCELED"))
        self.assertEqual(matches[1]["winner"], "home")

    def test_bracket_uses_explicit_schedule_winner_when_feed_omits_win_flag(self):
        key = "W.TEAM--------------"

        def fetch(path):
            if path.endswith("events/phases"):
                return [{"EvKey": key, "Desc": "Women"}]
            if "/groups-v2/" in path:
                return {"Groups": []}
            return [{"Code": "FNL", "Phases": [{"Code": key + ".QFNL", "Desc": "Quarterfinals", "Matches": [
                {"Info": {"Key": key + ".QFNL.000400--", "Status": "OFFICIAL"},
                 "Home": {"Org": "IND", "Res": "59"}, "Away": {"Org": "JPN", "Res": "57"}},
            ]}]}]

        records = [{
            "id": "CKT:" + key + ".QFNL.000400--",
            "matchup": "印度 vs 日本",
            "status": "OFFICIAL",
            "home": {"Winner": True},
            "away": {"Winner": False},
        }]
        match = get_tournament("CKT", records=records, fetcher=fetch)["events"][0]["rounds"][0]["matches"][0]
        self.assertEqual(match["winner"], "home")

    def test_unpublished_result_is_explicit(self):
        self.assertFalse(get_match_details("TEN:M.DOUBLES-----------.R32-.000100--", lambda _: None)["available"])

    def test_network_error_propagates_for_last_good_cache(self):
        def fail(_):
            raise SyncError("offline")
        with self.assertRaises(SyncError):
            get_match_details("TEN:M.DOUBLES-----------.R32-.000100--", fail)
        with self.assertRaises(SyncError):
            get_tournament("VVO", fetcher=fail)

    def test_handball_preallocated_halves_are_not_actual_scores(self):
        # The public HBL results endpoint allocates both 0-0 halves while
        # Status=SCHEDULED and CurrentPeriod=0.
        payload = {"Info": {"Type": "T", "Status": "SCHEDULED", "IsLive": False},
                   "Competitors": [{"Org": "HKG"}, {"Org": "KOR"}],
                   "Results": {"CurrentPeriod": 0, "Periods": [
                       {"Desc": "1st Half", "ResHome": "0", "ResAway": "0"},
                       {"Desc": "2nd Half", "ResHome": "0", "ResAway": "0"}]}}
        out = get_match_details("HBL:W.TEAM7-------------.FNL-.000100--", lambda _: payload)
        self.assertFalse(out["available"])
        self.assertEqual(out["sections"], [])

    def test_tennis_points_tiebreak_and_player_names(self):
        # Extension keys are the ones read by the official TENH2HPeriods
        # component; this also exercises integer zero instead of string zero.
        payload = {"Info": {"Type": "A", "Status": "RUNNING", "IsLive": True},
                   "Competitors": [{"Org": "CHN", "NameS": "PLAYER A", "Splits": [{"Result": 7}, {"Result": 0}]},
                                   {"Org": "JPN", "NameS": "PLAYER B", "Splits": [{"Result": 6}, {"Result": 0}]}],
                   "Results": {"CurrentPeriod": 2, "Periods": [
                       {"Desc": "Set 1", "Extensions": [
                           {"Type": "PERIOD_INFO", "Code": "HomeTieBreakPoints", "Value": 7},
                           {"Type": "PERIOD_INFO", "Code": "AwayTieBreakPoints", "Value": 4}]},
                       {"Desc": "Set 2"}], "Extensions": [
                           {"Type": "RESULT_INFO", "Code": "HomePoints", "Value": 0},
                           {"Type": "RESULT_INFO", "Code": "AwayPoints", "Value": 15}]}}
        out = get_match_details("TEN:M.SINGLES-----------.QFNL.000100--", lambda _: payload)
        self.assertEqual(out["home"], "PLAYER A（中国）")
        self.assertEqual(out["sections"][0]["rows"], [["第1盘", "7", "6"], ["第1盘抢七", "7", "4"], ["第2盘", "0", "0"]])
        self.assertEqual(out["sections"][1]["rows"], [["0", "15"]])

    def test_team_submatch_small_scores(self):
        payload = {"Info": {"Type": "T", "Status": "RUNNING"}, "Competitors": [{"Org": "CHN"}, {"Org": "JPN"}], "Results": {},
                   "SubUnits": [{"Info": {"Type": "A", "Status": "OFFICIAL", "UnitDescA": "Match 1"},
                                 "Competitors": [{"NameS": "A", "Org": "CHN"}, {"NameS": "B", "Org": "JPN"}],
                                 "Results": {"CurrentPeriod": 1, "Periods": [{"Desc": "Game 1", "ResHome": "11", "ResAway": "9"}]}}]}
        out = get_match_details("TTE:M.TEAM--------------.GPA-.00010000", lambda _: payload)
        self.assertTrue(out["available"])
        self.assertEqual(out["sections"], [])
        self.assertEqual(out["subMatches"][0]["sections"][0]["rows"][0], ["第1局", "11", "9"])
        self.assertEqual(out["subMatches"][0]["home"], "A（中国）")

    def test_table_tennis_and_badminton_lineup_members_include_official_photos(self):
        def payload(sport):
            return {
                "Info": {"Type": "T", "Status": "RUNNING", "IsLive": True},
                "Competitors": [
                    {"Org": "CHN", "Name": "China", "Reg": f"{sport}WTEAM-------CHN01", "Members": [
                        {"Reg": "14244548", "Org": "CHN", "Name": "FAN Shuhan", "NameS": "FAN S", "Substitute": False, "Captain": True, "Bib": "", "PosDesc": ""},
                        {"Reg": "16276085", "Org": "CHN", "Name": "CHEN Yi", "NameS": "CHEN Y", "Substitute": True, "Captain": False, "Bib": "7", "PosDesc": ""},
                    ]},
                    {"Org": "JPN", "Name": "Japan", "Reg": f"{sport}WTEAM-------JPN01", "Members": []},
                ],
                "Results": {},
                "SubUnits": [{
                    "Info": {"Key": "tie.00010001", "Type": "A", "Status": "START_LIST"},
                    "Competitors": [
                        {"Reg": "14244548", "Org": "CHN", "Name": "FAN Shuhan"},
                        {"Reg": "380921", "Org": "JPN", "Name": "HARIMOTO Miwa"},
                    ],
                    "Results": {},
                }],
            }

        for sport in ("TTE", "BDM"):
            out = get_match_details(f"{sport}:W.TEAM--------------.GPA-.00010000", lambda _, sport=sport: payload(sport))
            self.assertTrue(out["available"])
            self.assertEqual([p["name"] for p in out["homePlayers"]], ["FAN Shuhan", "CHEN Yi"])
            self.assertEqual(out["homePlayers"][0]["org"], "CHN")
            self.assertEqual(out["homePlayers"][0]["photo"], "https://results.asiangames2026.org/ag2026/photos/14244548.jpg")
            self.assertEqual(out["homePlayers"][0]["avatar"], out["homePlayers"][0]["photo"])
            self.assertTrue(out["homePlayers"][0]["captain"])
            self.assertTrue(out["homePlayers"][1]["substitute"])
            self.assertEqual(out["subMatches"][0]["homePlayers"][0]["reg"], "14244548")
            self.assertEqual(out["subMatches"][0]["awayPlayers"][0]["photo"], "https://results.asiangames2026.org/ag2026/photos/380921.jpg")

    def test_lineup_falls_back_to_individual_competitor_without_members(self):
        payload = {
            "Info": {"Type": "A", "Status": "OFFICIAL"},
            "Competitors": [
                {"Reg": "16276085", "Org": "CHN", "Name": "CHEN Yi"},
                {"Reg": "380921", "Org": "JPN", "Name": "HARIMOTO Miwa"},
            ],
            "Results": {},
        }
        out = get_match_details("TTE:M.SINGLES-----------.R32-.000100--", lambda _: payload)
        self.assertTrue(out["available"])
        self.assertEqual(out["homePlayers"][0]["name"], "CHEN Yi")
        self.assertEqual(out["awayPlayers"][0]["reg"], "380921")

    def test_team_submatches_keep_prestart_names_and_official_order(self):
        payload = {
            "Info": {"Type": "T", "Status": "RUNNING", "IsLive": True},
            "Competitors": [{"Org": "INA"}, {"Org": "MGL"}],
            "Results": {},
            # The endpoint has occasionally returned these in a non-numeric
            # order. SubMatchNum is the authoritative order.
            "SubUnits": [
                {"Info": {"Key": "W.TEAM.00010003", "Type": "A", "Status": "START_LIST", "UnitDescA": "Tie 4 Match 3"},
                 "Results": {"Extensions": [{"Type": "UNIT_INFO", "Code": "SubMatchNum", "Value": "3"}], "CurrentPeriod": 0,
                             "Periods": [{"Desc": "Game 1", "ResHome": "0", "ResAway": "0"}]},
                 "Competitors": [{"Name": "WIRYAWAN Thalita Ramadhani", "Org": "INA"}, {"Name": "KHERLENBAATAR Sarangua", "Org": "MGL"}]},
                {"Info": {"Key": "W.TEAM.00010002", "Type": "D", "Status": "RUNNING", "IsLive": True},
                 "Results": {"Extensions": [{"Type": "UNIT_INFO", "Code": "SubMatchNum", "Value": "2"}], "CurrentPeriod": 1,
                             "Periods": [{"Desc": "Game 1", "ResHome": "0", "ResAway": "0"}]},
                 "Competitors": [{"Name": "ROSE Rachel Allesya/SETIANINGRUM Febi", "Org": "INA", "Result": "0"},
                                 {"Name": "CHULUUNBAT Khulangoo/GANBAT Enkhjin", "Org": "MGL", "Result": "0"}]},
                {"Info": {"Key": "W.TEAM.00010001", "Type": "A", "Status": "OFFICIAL"},
                 "Results": {"Extensions": [{"Type": "UNIT_INFO", "Code": "SubMatchNum", "Value": "1"}], "CurrentPeriod": 1,
                             "Periods": [{"Desc": "Game 1", "ResHome": "11", "ResAway": "9"}]},
                 "Competitors": [{"Name": "WARDANI Putri Kusuma", "Org": "INA", "Result": "1"},
                                 {"Name": "TSELMEG-OD Enkhlen", "Org": "MGL", "Result": "0"}]},
            ],
        }
        out = get_match_details("BDM:W.TEAM--------------.8FNL.00040000", lambda _: payload)
        self.assertTrue(out["available"])
        self.assertEqual([child["number"] for child in out["subMatches"]], [1, 2, 3])
        first, doubles, third = out["subMatches"]
        self.assertEqual(first["type"], "单打")
        self.assertEqual((first["homeScore"], first["awayScore"]), ("1", "0"))
        self.assertEqual(doubles["type"], "双打")
        self.assertEqual(doubles["home"], "ROSE Rachel Allesya/SETIANINGRUM Febi（印度尼西亚）")
        self.assertEqual(doubles["homeScore"], "0")
        self.assertEqual(third["home"], "WIRYAWAN Thalita Ramadhani（印度尼西亚）")
        self.assertEqual((third["homeScore"], third["awayScore"]), ("", ""))
        self.assertEqual(third["sections"], [])

    def test_nested_team_submatches_are_kept_structured(self):
        payload = {"Info": {"Type": "T", "Status": "RUNNING"}, "Competitors": [], "Results": {},
                   "SubUnits": [{"Info": {"Key": "parent.00010001", "Type": "T", "Status": "RUNNING"},
                                 "Competitors": [], "Results": {},
                                 "SubUnits": [{"Info": {"Key": "child.00010001", "Type": "A", "Status": "START_LIST"},
                                                "Competitors": [{"Name": "Player", "Org": "CHN"}, {"Name": "Opponent", "Org": "JPN"}],
                                                "Results": {}}]}]}
        out = get_match_details("TTE:M.TEAM--------------.GPA-.00010000", lambda _: payload)
        self.assertEqual(out["sections"], [])
        self.assertEqual(out["subMatches"][0]["type"], "")
        self.assertEqual(out["subMatches"][0]["number"], 1)
        self.assertEqual(out["subMatches"][0]["subMatches"][0]["type"], "单打")

    def test_handball_pool_is_not_mislabelled_final_and_zero_is_preserved(self):
        key = "W.TEAM7-------------"
        def fetch(path):
            if path.endswith("events/phases"):
                return [{"EvKey": key, "Desc": "Women"}]
            if "/groups-v2/" in path:
                return {"Groups": [{"Key": key + ".FNL-", "Type": "POOL", "Desc": "Round", "isTeam": True,
                                    "Competitors": [{"Org": "JPN", "Pos": "1", "Rk": "", "Played": 0, "Points": 0, "Won": 0}]}]}
            return [{"Code": "FNL", "Desc": "Finals", "Phases": [{"Code": key + ".FNL-", "Desc": "Round", "Matches": [
                {"Info": {"Key": key + ".FNL-.000100--"}, "Home": {"Org": "JPN"}, "Away": {"Org": "CHN"}}]}]}]
        out = get_tournament("HBL", fetcher=fetch)["events"][0]
        self.assertEqual(out["rounds"], [])
        self.assertEqual(out["groups"][0]["rows"][0], ["—", "日本", "0", "0", "0"])

    def test_tennis_bracket_uses_verified_phase_code(self):
        def fetch(path):
            if path.endswith("events/phases"):
                return [{"EvKey": "M.DOUBLES-----------", "Desc": "Men's Doubles"}]
            if "/groups-v2/" in path:
                return {"Groups": []}
            return [{"Code": "FNL", "Phases": [{"Code": "M.DOUBLES-----------.R32-", "Desc": "Second Round", "Matches": [
                {"Info": {"Key": "M.DOUBLES-----------.R32-.000100--"}, "Home": {"Name": "TBD"}, "Away": {"Name": "TBD"}}]}]}]
        out = get_tournament("TEN", fetcher=fetch)["events"][0]
        self.assertEqual(out["rounds"][0]["name"], "32强赛")
        self.assertEqual(out["rounds"][0]["matches"][0]["home"], "待定")
        self.assertNotIn("nextMatchId", out["rounds"][0]["matches"][0])

    def test_handball_winner_advances_to_final_not_bronze(self):
        key = "M.TEAM7-------------"
        def origin(phase, rank, unit):
            return {"Extensions": [{"Type": "RESULT_INFO", "Code": code, "Value": value} for code, value in [
                ("ComesFromPhaseKey", key + "." + phase), ("ComesFromRank", rank), ("ComesFromUnitKey", key + "." + unit)]]}
        def fetch(path):
            if path.endswith("events/phases"):
                return [{"EvKey": key, "Desc": "Men"}]
            if "/groups-v2/" in path:
                return {"Groups": []}
            return [{"Code": "FNL", "Desc": "Finals", "Phases": [
                {"Code": key + ".QFNL", "Desc": "Quarterfinals", "Matches": [{"Info": {"Key": key + ".QFNL.000100--"},
                    "Home": origin("GPA-", "2", "GPA-.--------"), "Away": origin("GPB-", "3", "GPB-.--------")}]},
                {"Code": key + ".SFNL", "Desc": "Semifinals", "Matches": [{"Info": {"Key": key + ".SFNL.000100--"},
                    "Home": origin("QFNL", "1", "QFNL.000100--"), "Away": {}}]},
                {"Code": key + ".FNL-", "Desc": "Finals", "Matches": [{"Info": {"Key": key + ".FNL-.000100--"},
                    "Home": origin("SFNL", "1", "SFNL.000100--"), "Away": {}}]}]},
                {"Code": "BRN", "Desc": "Bronze", "Phases": [{"Code": key + ".FNL-", "Desc": "Bronze", "Matches": [
                    {"Info": {"Key": key + ".FNL-.000200--"}, "Home": origin("SFNL", "2", "SFNL.000100--"), "Away": {}}]}]}]
        rounds = get_tournament("HBL", fetcher=fetch)["events"][0]["rounds"]
        self.assertEqual(rounds[0]["matches"][0]["home"], "A组第2名")
        self.assertEqual(rounds[1]["matches"][0]["home"], "胜者：1/4决赛第1场")
        self.assertEqual(rounds[1]["matches"][0]["nextMatchId"], "HBL:" + key + ".FNL-.000100--")
        self.assertEqual(rounds[3]["matches"][0]["home"], "负者：半决赛第1场")


if __name__ == "__main__":
    unittest.main()
