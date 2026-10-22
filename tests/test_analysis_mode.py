"""“分析一下”应基于数据报告，而不是去查产品资料。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class AnalysisModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def _ask(self, question):
        return pipeline.answer(question, self.night, [], use_llm=False)

    def test_analysis_uses_data_report(self):
        result = self._ask("分析一下")
        self.assertEqual(result.mode, "数据分析")
        for section in ("数据概览", "主要发现", "建议", "与上一晚对比",
                        "最近 7 晚平均", "多晚趋势"):
            self.assertIn(section, result.text)
        self.assertIn("5.7", result.text)
        self.assertIn("18.1", result.text)
        self.assertIn("AHI 6.9 → 5.7", result.text)
        self.assertIn("400.86", result.text)

    def test_analysis_variants(self):
        for question in ("帮我分析一下", "解读一下这份报告", "看看数据"):
            self.assertEqual(self._ask(question).mode, "数据分析", question)

    def test_specific_metric_is_not_hijacked(self):
        result = self._ask("分析一下漏气")
        self.assertNotEqual(result.mode, "数据分析")
        self.assertIn("设备数据", result.mode)

    def test_advice_uses_current_night_data(self):
        result = self._ask("有什么建议")
        self.assertEqual(result.mode, "数据建议")
        self.assertIn("依据", result.text)
        self.assertIn("AHI 5.7", result.text)
        self.assertIn("P95 漏气 18.1", result.text)
        self.assertIn("检查头带", result.text)
        self.assertNotIn("产品资料里查到", result.text)

    def test_metric_advice_is_not_hijacked(self):
        result = self._ask("漏气有什么建议")
        self.assertNotEqual(result.mode, "数据建议")
        self.assertIn("设备数据", result.mode)


if __name__ == "__main__":
    unittest.main()
