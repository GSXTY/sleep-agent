"""问主机能否生成报告，不能触发 Agent 的单晚报告。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class ReportCapabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_device_report_question_goes_to_docs(self):
        result = pipeline.answer("主机也能生成报告吗", self.night, [], use_llm=False)
        self.assertTrue(result.mode.startswith("产品文档"), result.mode)
        self.assertIn("默认可显示", result.text)
        self.assertNotIn("单晚治疗报告（", result.text)

    def test_explicit_report_request_still_generates(self):
        result = pipeline.answer("帮我生成 2026-10-15 的报告", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "报告生成")
        self.assertIn("单晚治疗报告（", result.text)

    def test_scope_less_report_request_asks_first(self):
        result = pipeline.answer("帮我生成报告", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "范围确认")
        self.assertNotIn("单晚治疗报告（", result.text)

    def test_report_content_question_goes_to_docs(self):
        result = pipeline.answer("主机报告会显示哪些指标？", self.night, [], use_llm=False)
        self.assertTrue(result.mode.startswith("产品文档"), result.mode)
        self.assertIn("AHI", result.text)
        self.assertIn("P95", result.text)
        self.assertNotIn("单晚治疗报告（", result.text)

    def test_generate_with_metrics_still_generates(self):
        result = pipeline.answer("帮我生成报告，包含哪些指标", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "范围确认")


if __name__ == "__main__":
    unittest.main()
