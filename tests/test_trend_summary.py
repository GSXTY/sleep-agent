"""跨晚趋势：平均值、首末变化与走势。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class TrendSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_multi_night_change_uses_trend_summary(self):
        result = pipeline.answer("这几天数据有什么变化", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "趋势分析")
        self.assertIn("平均值", result.text)
        self.assertIn("首晚→末晚", result.text)
        self.assertIn("AHI", result.text)
        self.assertIn("走势", result.text)

    def test_metric_specific_trend_keeps_device_mode(self):
        result = pipeline.answer("最近 7 晚 AHI 趋势", self.night, [], use_llm=False)
        self.assertIn("设备数据", result.mode)
        self.assertNotEqual(result.mode, "趋势分析")

    def test_period_comparison_uses_multi_night_summary(self):
        result = pipeline.answer("对比一下最近几天的数据", self.night, [],
                                 use_llm=False)
        self.assertEqual(result.mode, "趋势分析")
        self.assertIn("平均值", result.text)
        self.assertIn("首晚→末晚", result.text)

    def test_comparison_is_not_trend_summary(self):
        result = pipeline.answer("上一次的报告和前一次相比", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "报告对比")


if __name__ == "__main__":
    unittest.main()
