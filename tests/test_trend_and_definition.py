"""趋势问法要给跨晚对比；指标定义优先给解释而不是产品文档原文。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class TrendAndDefinitionTest(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_dataset()[-1]

    def ask(self, question):
        return pipeline.answer(
            pipeline.UserQuery(question=question, use_llm=False), self.night)

    def test_trend_question_includes_multi_night_series(self):
        result = self.ask("最近 7 晚 AHI 趋势")
        self.assertIn("设备数据", result.mode)
        self.assertIn("多晚趋势", result.text)
        self.assertIn("7.3", result.text)      # 最早一晚的 AHI，单晚回答里没有
        self.assertIn("5.7", result.text)

    def test_plain_value_question_does_not_add_trend(self):
        result = self.ask("2026-10-15 的 AHI 是多少")
        self.assertIn("设备数据", result.mode)
        self.assertNotIn("按时间顺序 AHI 依次为", result.text)
        self.assertNotIn("7.3", result.text)

    def test_p95_definition_uses_metric_explanation(self):
        result = self.ask("P95 是什么意思")
        self.assertIn(result.mode.split("（")[0], ("产品文档", "指标解释"))
        self.assertIn("95%", result.text)
        self.assertIn("P95", result.text)

    def test_product_feature_definition_still_uses_docs(self):
        result = self.ask("双水平是什么意思")
        self.assertIn("产品文档", result.mode)
        self.assertNotIn("指标解释", result.mode)

    def test_ahi_definition_is_not_hijacked_by_docs(self):
        result = self.ask("AHI 是什么意思")
        self.assertIn(result.mode.split("（")[0], ("产品文档", "指标解释"))
        # 定义问题不应附带当晚数值
        self.assertNotIn("5.7", result.text)


if __name__ == "__main__":
    unittest.main()
