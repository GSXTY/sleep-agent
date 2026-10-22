"""App 报告板块解读：同源字段、结构化输出和合规表述。"""
import json
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline, report_insight


class AppReportInsightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()
        cls.payload = report_insight.build(cls.night, 30)

    def test_overview_has_five_renderable_modules(self):
        structured = self.payload["structured"]
        self.assertEqual(structured["schema_version"], "1.0")
        self.assertEqual(structured["report_type"], "app_report_insight")
        self.assertEqual(structured["selected_module"], "overall")
        self.assertEqual(
            [item["module"] for item in structured["insights"]],
            ["score", "events", "breathing", "leak", "pressure"],
        )
        for item in structured["insights"]:
            self.assertIn(item["level"], ("info", "attention", "data_gap"))
            self.assertIn("definition", item)
            self.assertIn("evidence", item)
            self.assertIn("source_fields", item)

    def test_evidence_reuses_raw_app_fields(self):
        leak = next(item for item in self.payload["structured"]["insights"]
                    if item["module"] == "leak")
        evidence = {item["source_field"]: item for item in leak["evidence"]}
        self.assertEqual(evidence["air_leakage_avg"]["value"],
                         self.night.raw["air_leakage_avg"])
        self.assertEqual(evidence["air_leakage_p95"]["display"], "18.1 L/min")
        self.assertIn("large_leak_minutes", evidence)

    def test_no_diagnostic_verdict_in_conclusions(self):
        for item in self.payload["structured"]["insights"]:
            self.assertNotIn("正常", item["conclusion"])
            self.assertNotIn("异常", item["conclusion"])
            self.assertNotIn("严重", item["conclusion"])
        self.assertIn("不构成医疗诊断",
                      self.payload["structured"]["disclaimer"])

    def test_module_detection_and_focused_payload(self):
        self.assertEqual(report_insight.detect_module("App报告里的漏气怎么理解"),
                         "leak")
        focused = report_insight.build(self.night, 30, "leak")["structured"]
        self.assertEqual(focused["selected_module"], "leak")
        self.assertEqual(len(focused["insights"]), 1)
        self.assertEqual(focused["insights"][0]["module"], "leak")

    def test_pipeline_returns_structured_report_insight(self):
        result = pipeline.answer(
            "解读一下这份报告", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "App报告解读")
        self.assertEqual(result.structured["report_type"], "app_report_insight")
        self.assertIn("【结论】", result.text)
        json.dumps(result.structured, ensure_ascii=False)

    def test_metric_report_question_is_focused(self):
        result = pipeline.answer(
            "App报告里的漏气怎么理解", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "App报告解读")
        self.assertEqual(result.structured["selected_module"], "leak")
        self.assertEqual(len(result.structured["insights"]), 1)
        self.assertIn("定义：漏气指标反映", result.text)

    def test_report_metric_definition_includes_definition(self):
        result = pipeline.answer(
            "报告里的AHI是什么意思", self.night, [], use_llm=False
        )
        self.assertEqual(result.structured["selected_module"], "events")
        self.assertIn("定义：AHI 是平均每小时", result.text)
        self.assertIn("5.7", result.text)

    def test_score_direction_question_is_corrected_from_data(self):
        result = pipeline.answer(
            "为什么我的评分下降了", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "评分分析")
        self.assertIn("记录显示评分没有下降", result.text)
        self.assertIn("84 分", result.text)

    def test_generate_report_is_not_hijacked(self):
        self.assertFalse(report_insight.should_handle("生成App报告"))
        result = pipeline.answer("生成 2026-10-15 的报告", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "报告生成")

    def test_analysis_also_carries_structured_insights(self):
        result = pipeline.answer("分析最近7天", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "数据分析")
        self.assertEqual(result.structured["report_type"], "app_report_insight")
        self.assertEqual(len(result.structured["insights"]), 5)

    def test_missing_module_is_data_gap(self):
        raw = {"record_date": "2026-10-15", "user_id": "DEMO-USER"}
        night = nightdata.Night(raw=raw, user_id="DEMO-USER", history=[])
        structured = report_insight.build(night, 30, "score")["structured"]
        self.assertEqual(structured["insights"][0]["level"], "data_gap")
        self.assertIn("没有提供", structured["insights"][0]["conclusion"])


if __name__ == "__main__":
    unittest.main()
