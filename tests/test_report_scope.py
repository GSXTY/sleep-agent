"""报告范围：未指定时先问；单晚、近 7 天、近 30 天分别生成。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class TestReportScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def _ask(self, question):
        return pipeline.answer(question, self.night, [], use_llm=False)

    def test_generic_report_asks_for_scope(self):
        result = self._ask("生成报告")
        self.assertEqual(result.mode, "范围确认")
        self.assertEqual(result.risk_flags, ["needs_scope_confirmation"])
        self.assertEqual({item["type"] for item in result.candidates}, {"scope"})
        self.assertEqual(len(result.candidates), 3)
        self.assertIn("单晚", result.text)
        self.assertIn("近 7 天", result.text)
        self.assertIn("近 30 天", result.text)
        self.assertNotIn("单晚治疗报告（", result.text)

    def test_generic_situation_asks_for_scope(self):
        result = self._ask("我的情况怎么样")
        self.assertEqual(result.mode, "范围确认")
        self.assertIn("最近一晚", result.text)
        self.assertIn("近 7 天", result.text)

    def test_scope_option_can_be_sent_back_as_next_question(self):
        first = self._ask("生成报告")
        follow_up = first.candidates[1]["value"]
        result = self._ask(follow_up)
        self.assertEqual(result.mode, "报告生成（周期）")
        self.assertIn("近 7 天治疗分析报告", result.text)

    def test_previous_date_does_not_skip_report_scope_question(self):
        history = [
            {"role": "user", "content": "2026-10-14 的使用情况"},
            {"role": "assistant", "content": "已读取该晚数据。"},
        ]
        result = pipeline.answer(
            "生成报告", self.night, history, use_llm=False
        )
        self.assertEqual(result.mode, "范围确认")

    def test_plain_week_reply_continues_scope_confirmation(self):
        history = [
            {"role": "user", "content": "生成报告"},
            {"role": "assistant", "content": "你希望生成哪一类报告？"},
        ]
        result = pipeline.answer("一周", self.night, history, use_llm=False)
        self.assertEqual(result.mode, "报告生成（周期）")
        self.assertIn("近 7 天治疗分析报告", result.text)

    def test_confirmed_single_night_report(self):
        result = self._ask("生成最近一晚的报告")
        self.assertEqual(result.mode, "报告生成")
        self.assertIn("单晚治疗报告（", result.text)

    def test_weekly_report_uses_existing_records(self):
        result = self._ask("这一周的报告")
        self.assertEqual(result.mode, "报告生成（周期）")
        self.assertIn("近 7 天治疗分析报告", result.text)
        self.assertIn("7 个有记录夜晚", result.text)
        self.assertIn("阶段对比", result.text)

    def test_monthly_report_uses_available_records(self):
        result = self._ask("最近一个月的报告")
        self.assertEqual(result.mode, "报告生成（周期）")
        self.assertIn("近 30 天治疗分析报告", result.text)
        self.assertIn("7 个有记录夜晚", result.text)
        self.assertIn("不能当作完整 30 天基线", result.text)

    def test_explicit_week_metric_situation_is_period_report(self):
        result = self._ask("本周漏气情况")
        self.assertEqual(result.mode, "报告生成（周期）")
        self.assertIn("近 7 天治疗分析报告", result.text)

    def test_single_night_report_still_works(self):
        result = self._ask("生成昨晚报告")
        self.assertIn("单晚治疗报告（", result.text)
        self.assertIn("截至本晚共", result.text)


if __name__ == "__main__":
    unittest.main()
