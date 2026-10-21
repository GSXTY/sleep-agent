"""多轮确认：用户回"是的"时接回上一轮的时间确认。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import datetime
import unittest

from sleepagent import nightdata, pipeline

HISTORY = [
    {"role": "user", "content": "我昨晚的ahi是多少"},
    {"role": "assistant",
     "content": "数据里没有昨晚（2026-10-20）的记录。最近一次使用是 2026-10-15。\n"
                "你是想看这一晚吗？也可以直接给日期。"},
]


class PendingConfirmationTest(unittest.TestCase):
    def test_yes_restores_previous_question_with_date(self):
        rewritten, note = pipeline.resolve_pending_confirmation("是的", HISTORY)
        self.assertEqual(rewritten, "我2026-10-15的ahi是多少")
        self.assertIn("2026-10-15", note)

    def test_variants_are_recognized(self):
        for answer in ("是", "对", "好的", "可以", "嗯", "是的，看这一晚"):
            rewritten, _ = pipeline.resolve_pending_confirmation(answer, HISTORY)
            self.assertIn("2026-10-15", rewritten, answer)

    def test_negative_or_new_question_is_untouched(self):
        for question in ("不是", "那 10-14 呢", "什么是 AHI"):
            rewritten, note = pipeline.resolve_pending_confirmation(question, HISTORY)
            self.assertEqual(rewritten, question)
            self.assertEqual(note, "")

    def test_no_history_is_untouched(self):
        rewritten, note = pipeline.resolve_pending_confirmation("是的", [])
        self.assertEqual(rewritten, "是的")
        self.assertEqual(note, "")

    def test_resolved_question_selects_that_night(self):
        rewritten, _ = pipeline.resolve_pending_confirmation("是的", HISTORY)
        selection = nightdata.resolve_night(
            nightdata.DEFAULT_USER_ID, nightdata.load_dataset(),
            rewritten, today=datetime.date(2026, 10, 21))
        self.assertEqual(selection.status, "resolved")
        self.assertEqual(str(selection.night.raw["record_date"]), "2026-10-15")


if __name__ == "__main__":
    unittest.main()
