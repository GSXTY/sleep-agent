"""YA70 后端行映射。"""
import unittest

from sleepagent import analysis, backend_ya70, pipeline


ROW = {
    "id": 11587,
    "userid": 10002,
    "recordeDate": "2026-10-15 00:00:00",
    "deviceid": "TEST-DEVICE-001",
    "deviceType": "70120",
    "treatModels": "2",
    "treatTime": 14,
    "treatScore": 32,
    "treatDurationScore": 2.0,
    "maskScore": 20.0,
    "respScore": 10.0,
    "ahi": 0.0,
    "apneaIndex": 0.0,
    "hypopneaIndex": 0.0,
    "centralApneaIndex": 0.0,
    "treatPressure": 400,
    "treatPressure95": 400,
    "tidalVolumeAvg": 439.29,
    "tidalVolume95": 500.0,
    "hzAvg": 13.36,
    "inspiredAirTimeAvg": 1.28,
    "airLeakageAvg": 1.29,
    "airLeakage95": 10.0,
    "airLeakageDurationMax": 0.0,
    "airLeakageDurationPercent": 0.0,
}


class BackendYa70Test(unittest.TestCase):
    def test_maps_fields_and_pressure_unit(self):
        record = backend_ya70.map_row(ROW, 10002)
        self.assertEqual(record["record_date"], "2026-10-15")
        self.assertEqual(record["treat_time_minutes"], 14)
        self.assertEqual(record["treat_score"], 32)
        self.assertEqual(record["treat_duration_score"], 2.0)
        self.assertEqual(record["mask_score"], 20.0)
        self.assertEqual(record["resp_score"], 10.0)
        self.assertEqual(record["treat_pressure_avg"], 4.0)
        self.assertEqual(record["treat_pressure_p95"], 4.0)
        self.assertEqual(record["air_leakage_p95"], 10.0)
        self.assertEqual(record["therapy_mode"], "AutoCPAP")
        self.assertEqual(record["pressure_unit"], "hPa")

    def test_builds_ordered_night_history(self):
        second = dict(ROW)
        second["id"] = 12129
        second["recordeDate"] = "2026-10-18 00:00:00"
        nights = backend_ya70.rows_to_nights([second, ROW], 10002)
        self.assertEqual(
            [item.raw["record_date"] for item in nights],
            ["2026-10-15", "2026-10-18"],
        )
        self.assertEqual(nights[1].history[0]["record_date"], "2026-10-15")
        self.assertTrue(nights[1].facts)

    def test_missing_counts_and_pressure_range_are_not_zero_or_repeated(self):
        night = backend_ya70.rows_to_nights([ROW], 10002)[0]
        fact_ids = {item.fid for item in night.facts}
        self.assertNotIn("events-total", fact_ids)
        pressure = next(item.text for item in night.facts
                        if item.fid == "pressure-main")
        self.assertNotIn("未提供 到 未提供", pressure)
        self.assertNotIn("设定范围为", pressure)
        self.assertIn("pressure-setting-missing", fact_ids)

    def test_gap_is_not_called_previous_night(self):
        first = dict(ROW)
        first["id"] = 1
        first["recordeDate"] = "2026-10-19 00:00:00"
        first["treatScore"] = 70
        first["treatDurationScore"] = 40
        first["treatTime"] = 240
        second = dict(ROW)
        second["id"] = 2
        second["recordeDate"] = "2026-10-21 00:00:00"
        nights = backend_ya70.rows_to_nights([first, second], 10001)
        result = analysis.analyze(nights[-1], 7)
        self.assertIn("上一条有记录夜晚（2026-10-19）", result["score"]["conclusion"])
        answer = pipeline.answer(
            pipeline.UserQuery("分析最近7天", use_llm=False, user_id="10001"),
            nights[-1],
        )
        self.assertIn("【与上一条有记录夜晚（2026-10-19）对比】", answer.text)


if __name__ == "__main__":
    unittest.main()
