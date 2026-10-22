"""设备评分趋势、分项拆解与建议。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import analysis, nightdata, pipeline


class ScoreAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_score_breakdown_aligns_components_with_same_night_data(self):
        result = analysis.analyze(self.night, 7)
        score = result["score"]
        self.assertEqual(score["current"], 84)
        self.assertEqual(score["previous"], 83)
        self.assertEqual(score["delta"], 1)
        components = {item["key"]: item for item in score["components"]}
        self.assertEqual(components["duration"]["delta"], 1)
        self.assertIn("396 分钟 → 402 分钟",
                      components["duration"]["evidence"])
        self.assertIn("AHI 6.9 次/小时 → 5.7 次/小时",
                      components["events"]["evidence"])
        self.assertTrue(result["recommendations"])

    def test_score_change_question_returns_explanatory_analysis(self):
        result = pipeline.answer(
            "我的设备评分有什么变化", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "评分分析")
        for text in ("最近 7 天评分", "评分分项解释",
                     "时长得分 58 分 → 59 分，上升 1 分",
                     "396 分钟 → 402 分钟",
                     "面罩得分 17 分 → 17 分，未变",
                     "呼吸事件得分 8 分 → 8 分，未变", "建议"):
            self.assertIn(text, result.text)
        self.assertEqual(
            result.structured["selected_module"], "score"
        )
        self.assertIn("breakdown", result.structured["insights"][0])

    def test_general_analysis_includes_score_breakdown(self):
        result = pipeline.answer("分析最近7天", self.night, [], use_llm=False)
        self.assertIn("评分构成分析", result.text)
        self.assertIn("| 设备评分 |", result.text)
        self.assertIn("较上一晚 83 分上升 1 分", result.text)

    def test_score_trend_uses_requested_window(self):
        result = pipeline.answer(
            "最近30天设备评分趋势", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "评分分析")
        self.assertIn("最近 30 天评分", result.text)
        self.assertIn("最近 30 天窗口", result.text)

    def test_total_score_week_question_uses_score_analysis(self):
        result = pipeline.answer(
            "我这一周的总分变化", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "评分分析")
        self.assertIn("最近 7 天评分", result.text)
        self.assertIn("最近 7 天窗口", result.text)

    def test_score_insight_keeps_breakdown_for_app(self):
        result = pipeline.answer(
            "报告里的评分为什么变化", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "App报告解读")
        self.assertEqual(result.structured["selected_module"], "score")
        self.assertIn("breakdown", result.structured["insights"][0])


if __name__ == "__main__":
    unittest.main()
