import unittest

from sync_service import normalize_unit


def official_unit(event, phase, phase_desc, unit_desc=None, **extra):
    unit = {
        "Key": f"{event}.{phase}.000100--",
        "Event": event,
        "EventDesc": "Men's Doubles",
        "Phase": f"{event}.{phase}",
        "PhaseDesc": phase_desc,
        "DateTimeRaw": "2026-09-27T10:00:00+09:00",
        "IsPhase": False,
        "Status": "PROVISIONAL",
    }
    if unit_desc is not None:
        unit["UnitDesc"] = unit_desc
    unit.update(extra)
    return unit


class OfficialStageTests(unittest.TestCase):
    def test_tennis_published_second_round_is_identified_by_draw_size(self):
        # Official 2026-09-27 daily feed: no R64 doubles entries; all are R32.
        for event in ("M.DOUBLES-----------", "W.DOUBLES-----------", "W.SINGLES-----------"):
            with self.subTest(event=event):
                unit = official_unit(event, "R32-", "Men's Doubles Second Round")
                result = normalize_unit(unit, "TEN")
                self.assertIsNotNone(result)
                self.assertEqual(result["stage"], "32强赛")
                self.assertEqual(result["rawPhase"], "Men's Doubles Second Round")
                self.assertEqual(result["phaseCode"], f"{event}.R32-")

    def test_tennis_progression_does_not_invent_round_numbers(self):
        for code, label, expected in (
            ("R64-", "First Round", "64强赛"),
            ("R32-", "Second Round", "32强赛"),
            ("8FNL", "Third Round", "16强赛"),
            ("QFNL", "Quarter-finals", "1/4决赛"),
            ("SFNL", "Semi-finals", "半决赛"),
        ):
            with self.subTest(code=code):
                record = normalize_unit(official_unit("M.SINGLES-----------", code, f"Men's Singles {label}"), "TEN")
                self.assertEqual(record["stage"], expected)

    def test_verified_stage_variants_across_seven_sports(self):
        cases = [
            ("TEN", "M.DOUBLES-----------", "R32-", "Men's Doubles Second Round", None, "32强赛"),
            ("BBL", "M.TEAM9-------------", "GPA-", "Men's Opening Round Group A", "Game 2", "小组赛 A组 · 第2场"),
            ("BBL", "M.TEAM9-------------", "SFNL", "Men's Super Round", "Game 15", "超级循环赛 · 第15场"),
            ("BBL", "M.TEAM9-------------", "SF5-", "Men's Placement Round", "Game 13", "排位赛 · 第13场"),
            ("CKT", "W.TEAM--------------", "QFNL", "Women's Quarterfinals", "Women's Quarterfinal 1", "1/4决赛 · 第1场"),
            ("VVO", "M.TEAM6-------------", "SF13", "Men's Classification Match 13th-16th", "Match 37", "第13至16名排位赛 · 第37场"),
            ("VVO", "M.TEAM6-------------", "SF5-", "Men's Classification Match 5th-8th", "7th Place Match", "第7名赛"),
            ("TTE", "M.TEAM--------------", "GPA-", "Men's Team First Stage -Group A", "Men's Team First Stage -Group A Match 1", "第一阶段 A组 · 第1场"),
            ("BDM", "W.SINGLES-----------", "8FNL", "Women's Singles Round of 16", None, "16强赛"),
            ("HBL", "M.TEAM7-------------", "GPA-", "Men Preliminary Round Group A", "Men Preliminary Round Group A Match 1", "预赛 A组 · 第1场"),
            ("HBL", "W.TEAM7-------------", "FNL-", "Round", "Round Match 2", "循环赛第2场"),
        ]
        for disc, event, phase, phase_desc, unit_desc, expected in cases:
            with self.subTest(disc=disc, phase=phase):
                self.assertEqual(normalize_unit(official_unit(event, phase, phase_desc, unit_desc), disc)["stage"], expected)

    def test_alternate_description_preserves_gold_medal_team_match(self):
        unit = official_unit("M.TEAM--------------", "FNL-", "Men's Team Final", UnitDescA="Men's Team Gold Medal Team Match")
        result = normalize_unit(unit, "BDM")
        self.assertEqual(result["stage"], "金牌赛")
        self.assertEqual(result["rawStage"], "Men's Team Gold Medal Team Match")

    def test_identity_and_source_day_survive_beijing_midnight_conversion(self):
        unit = official_unit("M.DOUBLES-----------", "R32-", "Men's Doubles Second Round", ResCode="official-result-key", PhaseOrder=2, DateTimeRaw="2026-09-27T00:30:00+09:00", Home={"Org": "CHN", "Result": "1"})
        result = normalize_unit(unit, "TEN")
        self.assertEqual(result["sourceDate"], "2026-09-27")
        self.assertEqual(result["date"], "2026-09-26")
        self.assertEqual(result["time"], "23:30")
        self.assertEqual(result["officialKey"], unit["Key"])
        self.assertEqual(result["eventCode"], unit["Event"])
        self.assertEqual(result["resCode"], "official-result-key")
        self.assertEqual(result["phaseOrder"], 2)
        self.assertEqual(result["home"]["Org"], "CHN")

    def test_ceremonies_are_not_added_as_matches(self):
        unit = official_unit("M.DOUBLES-----------", "VICT", "Men's Doubles Victory Ceremony")
        self.assertIsNone(normalize_unit(unit, "TEN"))


if __name__ == "__main__":
    unittest.main()
