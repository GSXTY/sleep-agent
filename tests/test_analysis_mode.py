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
        for section in ("先给结论", "数据概览", "主要发现", "建议",
                        "与上一晚对比", "阶段对比", "窗口统计", "值得回看"):
            self.assertIn(section, result.text)
        self.assertIn("不能当作完整 30 天基线", result.text)
        self.assertIn("5.7", result.text)
        self.assertIn("18.1", result.text)
        self.assertIn("| AHI | 6.9次/小时 | 5.7次/小时", result.text)
        self.assertIn("| 平均治疗压力 | 4.37hPa | 4.35hPa", result.text)
        self.assertIn("| P95 漏气 | 18.9L/min | 18.1L/min", result.text)
        self.assertIn("| 平均潮气量 | 7 | 450.14mL", result.text)
        self.assertIn("| 平均呼吸频率 | 7 | 15.14次/分钟", result.text)
        self.assertIn("400.86", result.text)

    def test_analysis_variants(self):
        for question in ("帮我分析一下", "解读一下这份报告", "看看数据",
                         "综合分析这几天的数据", "对比分析一下最近的记录",
                         "分析一下最近30天的趋势和阶段变化",
                         "分析最近30天数据"):
            self.assertEqual(self._ask(question).mode, "数据分析", question)

    def test_analysis_includes_comparison_and_multi_night_statistics(self):
        result = self._ask("分析一下最近一周的平均、变化，并和上一晚对比")
        self.assertEqual(result.mode, "数据分析")
        self.assertIn("【与上一晚对比】", result.text)
        self.assertIn("| 治疗时长 | 396分钟 | 402分钟 | +6.0分钟 |", result.text)
        self.assertIn("【阶段对比】", result.text)
        self.assertIn("【窗口统计】", result.text)
        self.assertIn("首晚→末晚", result.text)
        self.assertIn("平均值", result.text)
        self.assertIn("趋势折算", result.text)
        self.assertNotIn("完整 30 天基线", result.text)

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
