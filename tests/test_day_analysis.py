"""单日分析不自动扩展为历史对比；后续追问只回答用户要求。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class DayAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_day_analysis_does_not_include_previous_night_or_trend(self):
        result = pipeline.answer(
            "分析2026-10-15", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "单日分析")
        self.assertIn("单日数据分析（2026-10-15）", result.text)
        self.assertIn("评分构成", result.text)
        self.assertNotIn("与上一晚对比", result.text)
        self.assertNotIn("阶段对比", result.text)
        self.assertNotIn("最近 7 天评分", result.text)
        self.assertEqual({item["type"] for item in result.candidates}, {"followup"})

    def test_change_follow_up_only_returns_comparison(self):
        history = [
            {"role": "user", "content": "分析2026-10-15"},
            {"role": "assistant", "content": "单日数据分析（2026-10-15）"},
        ]
        result = pipeline.answer(
            "有什么变化", self.night, history, use_llm=False
        )
        self.assertEqual(result.mode, "报告对比")
        self.assertIn("| 指标 | 2026-10-14 | 2026-10-15 | 变化 |", result.text)
        self.assertNotIn("数据概览", result.text)
        self.assertNotIn("主要发现", result.text)

    def test_explicit_compare_follow_up_only_returns_comparison(self):
        result = pipeline.answer(
            "和前一天比较", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "报告对比")
        self.assertIn("2026-10-14", result.text)
        self.assertNotIn("最近 7 天评分", result.text)

    def test_week_follow_up_returns_trend_not_comparison(self):
        history = [
            {"role": "user", "content": "分析2026-10-15"},
            {"role": "assistant", "content": "单日数据分析（2026-10-15）"},
        ]
        result = pipeline.answer(
            "最近7天有什么变化", self.night, history, use_llm=False
        )
        self.assertEqual(result.mode, "趋势分析")
        self.assertIn("窗口统计", result.text)
        self.assertNotIn("报告对比：", result.text)


if __name__ == "__main__":
    unittest.main()
