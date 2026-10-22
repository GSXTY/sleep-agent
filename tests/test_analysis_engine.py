"""30 天统计层：阶段变化、波动、偏离日和数据不足降级。"""
import datetime as dt
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import analysis, nightdata, pipeline


def build_30_night_fixture():
    end = dt.date(2026, 10, 15)
    history = []
    for index in range(30):
        day = end - dt.timedelta(days=29 - index)
        if index == 9:
            continue
        ahi = 10.0 - index * 0.12
        if index == 20:
            ahi = 25.0
        history.append({
            "user_id": "DEMO-USER",
            "record_date": day.isoformat(),
            "treat_time_minutes": 380 + index * 2,
            "treat_score": 80,
            "treat_duration_score": 55,
            "mask_score": 16,
            "resp_score": 9,
            "treat_pressure_avg": 5.0 + (0.02 if index % 2 else 0),
            "treat_pressure_p95": 5.5,
            "treat_pressure_max": 6.0,
            "ahi": ahi,
            "apnea_index": 3.0,
            "hypopnea_index": 3.0,
            "central_apnea_index": 0.5,
            "apnea_count_total": 20,
            "hypopnea_count_total": 20,
            "ca_count_total": 3,
            "apnea_duration_total_sec": 200,
            "hypopnea_duration_total_sec": 220,
            "longest_event_free_minutes": 70,
            "tidal_volume_avg_ml": 450,
            "tidal_volume_p95_ml": 560,
            "respiratory_rate_avg": 15.0,
            "minute_ventilation_avg_lpm": 7.0,
            "minute_ventilation_p95_lpm": 8.3,
            "inspiratory_ratio_percent": 42,
            "expiratory_ratio_percent": 58,
            "inspiratory_time_avg_sec": 1.6,
            "air_leakage_avg": 12.0 + index * 0.08,
            "air_leakage_p95": 18.0 + index * 0.20,
            "air_leakage_max": 25.0,
            "large_leak_minutes": 5,
            "large_leak_percent": 1.2,
            "data_source": "30天测试数据",
        })
    return nightdata.Night(raw=history[-1], user_id="DEMO-USER",
                           history=history[:-1])


class AnalysisEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = build_30_night_fixture()

    def test_window_detection(self):
        self.assertEqual(analysis.detect_window_days("分析一下"), 30)
        self.assertEqual(analysis.detect_window_days("最近30天数据"), 30)
        self.assertEqual(analysis.detect_window_days("最近一个月"), 30)
        self.assertEqual(analysis.detect_window_days("最近一周"), 7)
        self.assertEqual(analysis.detect_window_days("两周变化"), 14)

    def test_comparison_target_does_not_change_analysis_night(self):
        nights = nightdata.load_dataset()
        selection = nightdata.resolve_night(
            "DEMO-USER", nights, "分析最近一周的平均、变化，并和上一晚对比"
        )
        self.assertEqual(selection.status, "resolved")
        self.assertEqual(selection.night.raw["record_date"], "2026-10-15")

    def test_statistics_and_conclusions_go_beyond_displaying_rows(self):
        result = analysis.analyze(self.night, 30)
        self.assertEqual(result["quality"]["record_count"], 29)
        self.assertTrue(result["period"]["available"])
        self.assertIn("conclusions", result)
        self.assertTrue(any("整体" in item for item in result["conclusions"]))
        self.assertTrue(any("波动" in item for item in result["conclusions"]))
        self.assertTrue(any("无法区分是未使用还是未上传"
                            in item for item in result["conclusions"]))
        self.assertEqual(result["summaries"]["ahi"]["direction"], "下降")
        self.assertEqual(result["summaries"]["air_leakage_p95"]["direction"], "上升")
        self.assertTrue(any(item["key"] == "ahi" for item in result["outliers"]))

    def test_pipeline_renders_analysis_first_then_evidence(self):
        result = pipeline.answer("分析一下最近30天", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "数据分析")
        self.assertIn("请求最近 30 天", result.text)
        self.assertIn("【先给结论】", result.text)
        self.assertIn("【阶段对比】", result.text)
        self.assertIn("【窗口统计】", result.text)
        self.assertIn("【值得回看】", result.text)
        self.assertIn("变异系数", result.text)
        self.assertIn("2026-10-06 的AHI", result.text)
        self.assertLess(result.text.index("【先给结论】"),
                        result.text.index("【窗口统计】"))

    def test_single_metric_trend_uses_requested_window(self):
        text = analysis.metric_summary_text(self.night, "ahi", 30)
        self.assertIn("最近 30 天窗口", text)
        self.assertIn("趋势判断为下降", text)
        self.assertIn("波动情况", text)


if __name__ == "__main__":
    unittest.main()
