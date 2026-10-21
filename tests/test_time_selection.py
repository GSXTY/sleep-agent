"""时间指代解析与追问。

原则：能算到具体某一晚就答；算不出来（或数据里没有）就问日期，
不能默默用「最近一晚」顶替，让用户误以为看到的就是自己问的那一晚。
"""
import unittest
from unittest import mock
import datetime

from sleepagent import llm, nightdata, pipeline, product_kb


class TestResolveNight(unittest.TestCase):
    def setUp(self):
        self.nights = nightdata.load_dataset()

    def _pick(self, question, today=None):
        selection = nightdata.resolve_night(
            nightdata.DEFAULT_USER_ID, self.nights, question, today=today)
        picked = str(selection.night.raw["record_date"]) if selection.night else None
        return selection.status, picked, selection.note

    def test_previous_day_wording(self):
        for question in ("前一天晚上的使用情况", "上一晚呢", "前一晚怎么样", "上一天的报告"):
            self.assertEqual(self._pick(question)[:2], ("resolved", "2026-10-14"), question)

    def test_calendar_words_follow_system_date(self):
        # 系统日期 2026-10-16 时，昨晚/前天/大前天正好落在数据里
        today = datetime.date(2026, 10, 16)
        self.assertEqual(self._pick("昨晚用了多久", today)[:2], ("resolved", "2026-10-15"))
        self.assertEqual(self._pick("前天用了多久", today)[:2], ("resolved", "2026-10-14"))
        self.assertEqual(self._pick("大前天怎么样", today)[:2], ("resolved", "2026-10-13"))

    def test_calendar_words_without_record_are_stale(self):
        # 系统日期 2026-10-20，数据只到 10-15：不能拿 10-15 冒充「昨晚」
        today = datetime.date(2026, 10, 20)
        for question, expected in (("昨晚用了多久", "2026-10-19"),
                                   ("昨天用了多久", "2026-10-19"),
                                   ("前天用了多久", "2026-10-18")):
            status, picked, note = self._pick(question, today)
            self.assertEqual(status, "stale_relative", question)
            self.assertIsNone(picked, question)
            self.assertIn(expected, note, question)

    def test_recent_word_still_uses_latest(self):
        self.assertEqual(self._pick("最近用了多久")[:2], ("resolved", "2026-10-15"))

    def test_vague_time_is_ambiguous(self):
        for question in ("那天的使用情况", "上周用了多久", "前些天睡得怎么样"):
            status, picked, _ = self._pick(question)
            self.assertEqual(status, "ambiguous", question)
            self.assertIsNone(picked, question)

    def test_product_question_with_vague_word_is_not_ambiguous(self):
        # 「上周」出现在产品问题里，不该被拦成时间确认
        status, picked, _ = self._pick("上周买的面罩怎么清洁")
        self.assertEqual(status, "none")
        self.assertEqual(picked, "2026-10-15")

    def test_missing_date(self):
        status, picked, _ = self._pick("2025-01-01 的报告")
        self.assertEqual(status, "missing_date")
        self.assertIsNone(picked)

    def test_dataset_dates(self):
        dates = nightdata.dataset_dates(self.nights)
        self.assertEqual(dates[0], "2026-10-09")
        self.assertEqual(dates[-1], "2026-10-15")
        self.assertEqual(len(dates), len(self.nights))


class TestTimeClarification(unittest.TestCase):
    def setUp(self):
        self.nights = nightdata.load_dataset()
        self.night = self.nights[-1]
        self.dates = nightdata.dataset_dates(self.nights)

    def _ask(self, question, use_llm=False):
        selection = nightdata.resolve_night(
            nightdata.DEFAULT_USER_ID, self.nights, question,
            today=datetime.date(2026, 10, 20))
        request = pipeline.UserQuery(
            question=question, use_llm=use_llm,
            time_status=selection.status, time_note=selection.note,
            available_dates=self.dates,
        )
        return pipeline.answer(request, selection.night or self.night)

    def test_ambiguous_time_asks_for_date(self):
        with mock.patch.object(llm, "chat", side_effect=AssertionError("不应调用大模型")), \
                mock.patch.object(product_kb, "retrieve",
                                  side_effect=AssertionError("不应触发检索")):
            result = self._ask("那天的使用情况", use_llm=True)
        self.assertEqual(result.mode, "时间确认")
        self.assertIn("那天", result.text)
        self.assertIn("对应不到具体某一晚", result.text)
        self.assertIn("2026-10-09", result.text)
        self.assertNotIn("治疗时长", result.text)
        self.assertEqual(result.risk_flags, ["needs_date_confirmation"])
        self.assertTrue(result.validation["ok"])

    def test_missing_date_asks_for_another_date(self):
        result = self._ask("2025-01-01 的报告")
        self.assertEqual(result.mode, "时间确认")
        self.assertIn("没有 2025-01-01", result.text)
        self.assertIn("2026-10-09", result.text)
        self.assertNotIn("平均治疗压力", result.text)

    def test_vague_period_without_report_word(self):
        result = self._ask("上周用了多久")
        self.assertEqual(result.mode, "时间确认")
        self.assertIn("上周", result.text)

    def test_yesterday_without_record_asks_about_last_use(self):
        result = self._ask("昨晚的使用情况")
        self.assertEqual(result.mode, "时间确认")
        self.assertIn("数据里没有昨晚（2026-10-19）", result.text)
        self.assertIn("最近一次使用是 2026-10-15", result.text)
        self.assertEqual(result.candidates[0]["title"], "上一次使用（2026-10-15）")
        self.assertEqual(result.candidates[0]["value"], "2026-10-15的使用情况")

    def test_last_use_candidate_resolves_to_that_night(self):
        selection = nightdata.resolve_night(
            nightdata.DEFAULT_USER_ID, self.nights, "2026-10-15的使用情况")
        self.assertEqual(selection.status, "resolved")
        self.assertEqual(str(selection.night.raw["record_date"]), "2026-10-15")

    def test_previous_day_is_answered_with_that_night(self):
        result = self._ask("前一天晚上的使用情况")
        self.assertIn("设备数据", result.mode)
        self.assertIn("2026-10-14", result.text)

    def test_previous_day_report_uses_that_night(self):
        result = self._ask("前一天的晚上报告")
        self.assertEqual(result.mode, "报告生成")
        self.assertIn("2026-10-14", result.text)

    def test_explicit_date_still_works(self):
        result = self._ask("2026-10-11 的报告")
        self.assertEqual(result.mode, "报告生成")
        self.assertIn("2026-10-11", result.text)


if __name__ == "__main__":
    unittest.main()
